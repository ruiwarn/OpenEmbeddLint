# OpenEmbeddLint

[English](README.md) | 简体中文

面向嵌入式工程的 C/C++ 验证 CLI 和增量质量门禁。通过一个入口运行 clang-tidy、
Cppcheck、可选的 MISRA-C:2012 检查、clang-format 和 Lizard。
开发者、CI 和 AI 编程工具共用同一套检查核心。

## 功能

- 使用真实编译数据库，保留目标、ABI、宏定义、头文件和响应文件上下文。
- 完整扫描前后版本，区分新增、恶化和未变化的问题；存量违规不能抵消新增违规。
- 明确区分违规、执行错误、跳过和证据不足。
- 接入工程声明的宿主构建、测试、ASan/UBSan、LLVM 覆盖率及显式模糊测试。
- 使用专用的平台工具目录，分析器缺失时不会悄悄改用 PATH 中的其他版本。

内置检查在本机执行。工具安装从上游下载软件包，普通扫描不会下载或更新工具。
工程声明的命令具有执行权限，运行陌生仓库前需要像审查构建脚本一样审查配置。

## 安装

需要 Python 3.10+，增量门禁还需要 Git。先安装
[pipx](https://pipx.pypa.io/stable/installation/)，然后执行：

```sh
pipx install https://github.com/ruiwarn/OpenEmbeddLint/releases/download/v3.0.0/openembeddlint-3.0.0-py3-none-any.whl
openembeddlint --version
openembeddlint-tools install
openembeddlint --doctor
```

CLI 安装包体积较小。独立的工具安装器使用随包清单中的准确上游链接和 SHA-256，
只安装本机平台，并验证核心工具能否启动。已有安装直接复用和验证，不重复下载；
不完整的工具安装会明确报错，不会自动覆盖。公开版本通过 GitHub Releases 分发，
尚未发布到 PyPI。

也可以从源码安装：

```sh
git clone https://github.com/ruiwarn/OpenEmbeddLint.git
cd OpenEmbeddLint
pipx install --editable .
openembeddlint-tools install
openembeddlint --doctor
```

源码安装会将命令链接到当前仓库，后续 `git pull --ff-only` 即可更新实现。
依赖或打包元数据变化时，需要重新安装。

不用 pipx 时，将 wheel 或源码安装到 Python 虚拟环境，使用该环境的命令，
不需要替换系统 Python。

## 工具下载

| 组件 | 固定版本 | 上游链接 |
|---|---|---|
| LLVM / clang-tidy / clang-format / 覆盖率工具 | 23.1.4 | [LLVM](https://github.com/llvm/llvm-project/releases)、[conda-forge](https://anaconda.org/conda-forge/clang-tools/files) |
| Cppcheck 与 MISRA 检测器 | 2.22.0 | [Cppcheck](https://github.com/cppcheck-opensource/cppcheck/releases)、[conda-forge](https://anaconda.org/conda-forge/cppcheck/files) |
| Windows 宿主编译器及 sanitizer/fuzzer 运行库 | llvm-mingw 20261020 | [固定版本 UCRT x64 下载](https://github.com/mstorsjo/llvm-mingw/releases/download/20261020/llvm-mingw-20261020-ucrt-x86_64.zip) |
| CMake / Ninja | 4.4.5 / 1.13.2 | [CMake](https://cmake.org/download/)、[Ninja](https://github.com/ninja-build/ninja/releases) |
| 安装引导程序 | micromamba 2.10.0-0 | [micromamba](https://github.com/mamba-org/micromamba-releases/releases/tag/2.10.0-0) |
| Lizard | 1.25.0 | [PyPI](https://pypi.org/project/lizard/1.25.0/)，已携带其小体积 MIT Python 依赖 |

[tools/manifest.json](tools/manifest.json) 记录各平台的软件包下载链接、校验值、
二进制布局和历史验证证据。原生工具二进制、SDK 不进入 Git 或 Python 发布包。
上游组件保持各自许可证，详见 [THIRD_PARTY.md](THIRD_PARTY.md)。

安装后的默认工具目录：

| 系统 | 目录 |
|---|---|
| Windows | `%LOCALAPPDATA%/OpenEmbeddLint/tools/platforms/<platform>` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/openembeddlint/tools/platforms/<platform>` |
| macOS | `~/Library/Application Support/OpenEmbeddLint/tools/platforms/<platform>` |

平台标识为 `windows-x86_64`、`linux-x86_64`、`linux-arm64`、
`macos-x86_64`、`macos-arm64`。Linux 以 glibc 2.17 为基线，macOS 以
macOS 11 为基线。只对有本机运行证据的平台声明已实测，文件可用不等于运行通过。
macOS 宿主编译还需要系统的 Xcode Command Line Tools SDK；
Windows 使用 llvm-mingw，不要求安装 Visual Studio。

复用独立工具时，将 `OPENEMBEDDLINT_TOOLS_DIR` 设为包含平台目录的绝对路径。
源码入口也可以复用已有的 `tools/platforms/`。升级 Python CLI 会保留用户工具缓存，
工具版本升级需要按照发布要求单独处理。

## 快速使用

```sh
openembeddlint --project-root . -f src/main.c --compdb build/compile_commands.json
openembeddlint --project-root . --git-modified
```

还支持重复 `-f`、`-d`、`--git-staged`、`--git-all`。Git 选择器默认对比
HEAD，`--baseline <commit>` 可以更换基线，也可以为指定文件扫描开启增量比较。
单独使用 `-f`/`-d` 且不指定基线时，严格检查整个选中文件。

增量检查分别完整分析捕获的前后版本，两侧应用当前策略，不只是过滤修改行。
头文件修改通过真实包含关系扩展到编译单元。归因不明确或分析不完整时不能通过。
未变化的存量问题仍保留在完整报告中，即使本次门禁通过。
扫描不会修改实际源码或 Git 暂存区。

## 配置与报告

- `.clang-tidy`：原生检查规则、选项和警告/错误策略。
- `.c-verify.json`（schema 2）：编译上下文、Cppcheck、复杂度阈值、经评审的屏蔽及工程命令。

现有流程文件名继续使用 `.c-verify.json`，不增加第二份配置。
详见[工程配置](references/configuration.md)和[报告契约](references/report-contract.md)。

`quick` 执行静态检查和已配置的工程规则命令；`full` 增加声明的构建、测试和覆盖率；
`fuzz` 只执行显式模糊测试。`quick` 也能执行工程命令，陌生配置仍需审查。

```sh
openembeddlint --project-root . --git-modified --format text
openembeddlint --project-root . --git-modified --detail
```

默认紧凑 JSON 中，`checks` 是数量；`--detail` 才包含检查数组、位置、规则编号、
严重级别和证据。完整 `report.json` 和日志保留在唯一的外部运行目录。
`--output-dir` 指定其父目录，该目录必须位于源码仓库之外。

| 退出码 | 含义 |
|---|---|
| 0 | 请求的检查或门禁通过 |
| 1 | 发现阻断问题或验证失败 |
| 2 | 工具、执行、配置或编译上下文错误 |
| 3 | 必需的验证或归因证据不完整 |

`complete` 表示执行证据是否完整，不代表业务或硬件正确。
增量报告分别保留完整扫描状态和本次门禁状态。

## MISRA-C:2012

```sh
openembeddlint --project-root . --git-modified --misra-c2012
```

也可以开启 `static.misra_c2012`。开源 Cppcheck 检测器检查选中的 C 编译单元及其头文件，
只提供部分自动检查，不能作为完整合规认证。违规按错误阻断，参与增量比较；
带理由、限定范围的屏蔽继续保留审计记录。

公开版本不分发 MISRA 规则原文，不提供原文也能检测并显示规则编号。如有单独授权的文本，
将 `misra-c2012-rule-texts.txt` 放在上述系统 OpenEmbeddLint 数据目录中、与 `tools/` 同级；
或将 `OPENEMBEDDLINT_MISRA_RULE_TEXTS` 设为其绝对路径。
框架自动为所有工程加载，用到时复制进仓库外的运行产物，并在增量检查中记录指纹，
工程不需要配置文本路径。

## AI 技能与开发维护

[SKILL.md](SKILL.md) 是 AI 使用入口。通常将仓库安装到
`~/.agents/skills/openembeddlint`，Claude 的技能入口可以链接到同一份源码。
CLI 和技能共用实现，现有自动化也可以继续通过 `python scripts/c_verify.py` 调用。

工具安装完成后运行：

```sh
openembeddlint --self-test --timeout 600
```

源码目录也支持 `python scripts/c_verify.py --self-test --timeout 600`。
每个声明支持的平台都应运行本机测试；安装后 wheel 验证与源码自测分别记录。
CI 的打包检查不能代替分析器、固件或硬件验收。

OpenEmbeddLint 自身代码使用 [MIT 许可证](LICENSE)。
