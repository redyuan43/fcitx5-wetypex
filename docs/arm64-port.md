# arm64 移植进展（NX6）

记录把 WeTypeX 从 x86_64 搬到 arm64（Jetson Orin NX，Ubuntu 24.04）过程中的改动、
已解决的问题，以及当前卡点。所有代码改动都在本仓库的工作分支上，未推送上游。

## 结论先说

引擎现在能在 arm64 上**加载、跑完所有 C++ 全局构造、初始化成功、输出日志、读写配置**，
C++ 异常也能正常抛出并被 catch。卡在**用户词典初始化阶段的一次 `std::string` 对象
损坏**（见文末），此时后端进程会 SIGSEGV，fcitx5 因此拿不到语音服务。

## 为什么不能直接用 x86 那套

x86 版本能跑，是因为 macOS 的 x86_64 与 Linux 的 x86_64 在下面这些地方**恰好一致**，
换到 arm64 就全部暴露：

| 差异点 | x86_64 | arm64 |
| --- | --- | --- |
| 可变参数 | 都在栈上 | **全部在栈上**（AAPCS64 本应前 8 个走 x2–x7） |
| `std::string` 布局 | 两套布局一样 | Apple 用 alternate layout（数据在 offset 0、长度在 offset 23） |
| 构造函数 ABI | 构造后 `rax` 恰好还被用着 | Itanium ABI 要求 ctor 返回 `x0 = this`，Linux libc++ 不保证 |
| 栈探测 | `__chkstk` 用 `rax` | `___chkstk_darwin` 用 **x9**，且只探页、不动 sp |
| 栈展开 | 把 RBP 链翻译成 FDE 就够 | `__unwind_info` 是 compact 编码，libgcc 完全不认 |

## 代码改动

### `CMakeLists.txt`

1. 去掉 x86_64 硬拦截。
2. arm64 上用 `_LIBCPP_ABI_VERSION=2`（alternate string layout）构建的 libc++，
   通过 `-DWETYPE_LIBCXX_ABI2_PREFIX=/opt/libcxx-abi2` 指定头文件与库：

```cmake
if(CMAKE_SYSTEM_PROCESSOR MATCHES "aarch64|arm64")
  if(WETYPE_LIBCXX_ABI2_PREFIX)
    set(WETYPE_LIBCXX_ABI_FLAGS -nostdinc++ -isystem ${WETYPE_LIBCXX_ABI2_PREFIX}/include/c++/v1)
    set(WETYPE_LIBCXX_ABI_LINK_FLAGS -L${WETYPE_LIBCXX_ABI2_PREFIX}/lib
        -Wl,-rpath,${WETYPE_LIBCXX_ABI2_PREFIX}/lib -lc++abi)
  endif()
endif()
```

### `src/backend/macos_host.cpp`

按问题分类，每处都在注释里写清了“为什么”，这里只列要点：

1. **构造函数 trampoline**（`return_this_trampoline`）
   导入的 ctor/dtor 一律包一层汇编，返回前把 `x0` 设回 `this`。
   没有它，`recursive_mutex` 构造完紧接着的 `strb wzr, [x0, #64]` 会写到地址 0。

2. **Apple 变参**（`shim_*_apple` + `apple_vformat`）
   `printf/fprintf/sprintf/snprintf/vasprintf/vsprintf/vfprintf/vprintf` 全部走自研
   formatter。`sprintf` 曾经错误地跳进 `shim_snprintf_apple`（4 参数版），把栈地址
   当格式串、把容量当天文数字，导致**无界写入栈缓冲区**——这是最难找的一个 bug。

3. **栈探测**（`shim_chkstk`）
   ```asm
   shim_chkstk:
     mov x0, x9        ; 帧大小在 x9，不在 x0
     b shim_chkstk_impl
   ```
   `shim_chkstk_impl` 只逐页触碰，不动 sp（调用方自己 `sub sp`）。

4. **栈展开表**（`setup_unwind` + `tools/gen_unwind_ehframe.py`）
   把 `__unwind_info` 翻译成标准 `.eh_frame`，映射到固定地址 `0x103A00000`，
   personality 槽在 `0x103F00000`。
   **注意**：libgcc 的注册表是互斥的——`__register_frame` 内部只有一个 static
   `struct object`，注册第二张表会**静默顶掉**第一张。所以必须用
   `__register_frame_info` 并给每张表各自的对象块，或者干脆只注册一张。

5. **libc++ 外联成员补齐**
   Apple 的 libc++ 把这些做成外联符号，libc++ 18 已经内联或删除：
   `basic_string::__grow_by`、`C1ERKS5_`（拷贝构造）、`aSERKS5_`（拷贝赋值）、
   `6assignEPKc(m)`、`5eraseEmm`、以及 `wstring` 的对应项。全部在 `overrides`
   表里用普通函数接住，内部转调当前 libc++ 的公开 API。

6. **`___chkstk_darwin` 双下划线**
   Mach-O 里是 `___chkstk_darwin`（3 下划线），x86 时代注册的是 4 下划线，两个都要注册。

7. **bwrap 脚本**（`scripts/fcitx5-wetypex-{backend,account,ai}.sh.in`）
   arm64 没有 `/lib64`，改成条件绑定。

### `tools/`

- `prepare_macos_runtime_arm64.py`：包装上游脚本，只替换 `CPU_TYPE`，抽 arm64 slice。
  同一份二进制里的两个 slice 是同一个引擎构建，其余逻辑通用。
- `gen_unwind_ehframe.py`：`__unwind_info` → `.eh_frame` 生成器。用法：

```sh
llvm-objdump --macho --unwind-info image.macho > unwind.txt
tools/gen_unwind_ehframe.py unwind.txt ~/.local/share/fcitx5-wetypex/runtime/unwind.ehframe
```

输出对当前镜像为 `frames=61901 frameless=17890 functions=82381 lsda=22351 bytes=2595074`，
与实测可用的表**逐字节一致**。

## 踩坑清单（这一轮新增）

1. `__register_frame` 只能注册一张表，第二张会顶掉第一张。症状极具迷惑性：
   启动时探针能查到 FDE，真正抛异常时 `_Unwind_Find_FDE` 全返回 NULL。
2. CIE 里 personality 指针的 pcrel32 必须相对**该字段自身**的地址：
   偏移是 `4(record length) + len(cie body) + 1(aug length uleb) + 1(enc byte)`。
   算错 1 字节 → `blr` 跳到野地址，看起来像“随机崩溃”。
3. `_Unwind_Find_FDE` 的第二个参数不能传 NULL，libgcc 会无条件往里面写。
4. `sprintf` 的 trampoline 参数位移写错会**静默破坏栈**，表现为后续 `std::string`
   的指针字段变成一段文本（本例是 `"h.conf"`）。
5. ctor 过滤窗口 `WETYPE_CTOR_LO/HI`、crash handler 的 `MAPS` 输出是排查这类问题的
   主力工具，别删。

## 当前卡点

`serve` 模式初始化到用户词典阶段时，`ResourceMgr::Impl::CreateUserDict` 里
（镜像地址 `0x100be6e00` 附近）会从一个 `std::string` 对象读到**指向文本的“指针”**：

```
FAULT signal=11 address=0x666e6f632e68   ; "h.conf"
lr=0x100be6f28  ->  bl __init_copy_ctor_external
x1=0x666e6f632e68  x2=29
```

已排除：

- 不是 `insert` 的问题（用相同参数单独测 libc++，对象完全正常）；
- 不是 `__grow_by` 的问题（该路径走的是 `__grow_by_and_replace`，且日志里
  `n_add` 恒为 0）；
- 不是变参格式化越界（`CreateUserDict` 内没有任何 printf/memcpy 调用）；
- 不是 `sprintf`（该 bug 已修，损坏依旧）。

下一步建议：在 `0x100be6f10` 下断点需要等镜像映射完成后才能插（gdb 直接
`break *0x100be6f10` 会失败），可以先 `break main`，再用
`break *(main - 0x82d4 + 0x10db0)`（`setup_unwind`）作为“镜像已映射”的锚点，
在它的 commands 里再插目标断点。或者给 `std::string` 对象加硬件写断点，
定位是谁把 8 字节文本写进了对象头。
