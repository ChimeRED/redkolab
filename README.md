# 红红子lab (REDKO_LAB)

[![Release](https://github.com/ChimeRED/redkolab/actions/workflows/release.yml/badge.svg)](https://github.com/ChimeRED/redkolab/actions/workflows/release.yml)
[![最新版本](https://img.shields.io/github/v/release/ChimeRED/redkolab)](https://github.com/ChimeRED/redkolab/releases/latest)

一个 **Blender 4.0 ~ 最新版通用的单文件插件**，把三个高频操作变成一次点击：

| 功能 | 说明 |
| --- | --- |
| 🔧 删除所有材质 | 一键清空选中物体的全部材质槽（材质数据块本身保留在文件里） |
| ✅ 应用所有修改器 | 一键把所有修改器烘焙进网格 |
| 🗑 删除所有修改器 | 一键丢弃全部修改器（不烘焙） |
| 🎨 主题颜色 | 在 N 面板里改 Blender 界面主题颜色（35 项颜色 + 5 套视口预设） |
| ↕ 面板顺序 | 拖动调整 N 面板顺序：本插件各分块 + 整个侧栏的标签页（带一键还原） |

三种呼出方式：

1. **饼菜单** —— 默认快捷键 <kbd>Ctrl</kbd>+<kbd>E</kbd>（可改成任意按键）—— **只有上面 1~3 三个功能**
2. **N 面板** —— 3D 视图 ▸ 侧栏（<kbd>N</kbd>）▸ `红红子lab` 标签页 —— 里面是 5 个独立面板：
   主面板（三个功能按钮）+ **主题颜色 / 饼菜单与快捷键 / 插件设置 / 面板顺序** 四个可拖动排序的面板
3. 搜索菜单（<kbd>F3</kbd>）里直接搜「红红子lab」或「REDKO」

> 「插件设置」原来在饼菜单里，已按需求挪到 N 面板；主题颜色也只放 N 面板。
> 这两条都有自动化测试盯着（直接解析安装后的 `ui.py` 断言饼菜单只剩三个功能入口），改坏会立刻失败。

插件列表里显示的名字是 **红红子lab (REDKO_LAB)**，中英文都能搜到。

---

## 一、安装：一个 zip 通吃 4.0 ~ 最新版

只有一份安装包，从 **[Releases 页面](https://github.com/ChimeRED/redkolab/releases/latest)** 下载
`redkolab-<版本>.zip` —— 每个版本一条永久归档，附件里还有 `SHA256SUMS.txt` 可校验。

本地自己打也可以：`python tools/build_zips.py`，产物在 `dist/`（`dist/` 不入库，安装包一律由 Release 归档）。

| 你的 Blender | 怎么装 |
| --- | --- |
| **4.2 及以上**（4.2 / 4.3 / 4.4 / 4.5 / 5.x） | 偏好设置 ▸ **扩展 / Get Extensions** ▸ 右上角下拉 ▸ **Install from Disk**，选这个 zip；或者**直接把 zip 拖进 Blender 窗口** |
| **4.0 / 4.1** | 偏好设置 ▸ **插件 / Add-ons** ▸ 安装… ▸ **Install from Disk** |

> 同一个包在 4.2+ 的**插件页**也能装（会按传统插件方式加载，功能完全一样），
> 所以无论你习惯点哪个安装按钮都不会翻车。

装完在列表里勾选启用即可。**手动安装**：把压缩包里的 `redkolab/` 整个文件夹拷到

```
<Blender 配置目录>/scripts/addons/
```

Windows 一般是 `%APPDATA%\Blender Foundation\Blender\<版本>\scripts\addons\`。

### 为什么一个包能通吃

包里的结构只有一种，源码只有一份：

```
redkolab/
    __init__.py            <- 同时带 bl_info（传统插件元数据）
    blender_manifest.toml  <- 扩展清单（4.2+ 读这个）
    compat.py
    ops.py
    ui.py
```

4.2+ 的扩展安装器既能读根目录的清单、也能读单层目录里的清单；而 4.0/4.1 的传统安装器
只认「和模块同名的顶层目录」。把代码和清单一起放进 `redkolab/`，两种安装器就都满足了。

这是实测比出来的，不是猜的：

| 包内布局 | 4.0/4.1 插件页 | 4.2+ 插件页 | 4.2+ 扩展页 |
| --- | --- | --- | --- |
| 代码放根目录 + 目录副本 | 可以 | **报错** | 可以 |
| **只有目录（本包采用）** | 可以 | 可以 | 可以 |

> 根目录带 `__init__.py` 的包会被 4.2+ 的插件页安装直接拒绝
> （`ZIP packaged incorrectly; __init__.py should be in a directory`），
> 这是 Blender 对扩展包的固有行为，不是本插件的问题。

---

## 二、使用

**前提**：物体模式 + 选中至少一个物体。没有选中任何物体时，会自动作用在**活动物体**上。

### 饼菜单

在 3D 视图里按 <kbd>Ctrl</kbd>+<kbd>E</kbd>，三个方向：

```
左：删除所有材质      右：应用所有修改器
            下：删除所有修改器
```

（原来的「插件设置」入口已挪到 N 面板的「插件设置」子面板。）

> 注意：<kbd>Ctrl</kbd>+<kbd>E</kbd> 在**编辑模式**下是 Blender 自己的「边」菜单，
> 模式专用键位的优先级更高，所以饼菜单在编辑模式下不会弹出（物体模式下正常）。
> 想在任何模式都能用，可以在插件偏好设置里换一个不冲突的键。

### N 面板

3D 视图 ▸ 按 <kbd>N</kbd> ▸ `红红子lab` 标签页。除了三个按钮，还会实时显示
「选中 N 个物体 · 材质槽 X · 修改器 Y」，以及「呼出饼菜单」按钮（不方便按键时用）。

### 主题颜色（N 面板）

N 面板 ▸ `红红子lab` ▸ **「主题颜色」** 面板（在 N 面板里是独立的一块，不用展开别的东西）：

1. 下拉选一个颜色项（按分组列出：视口 / 界面 / 面板（5.x）/ 节点 / 动画）
2. 拖颜色选择器 —— **实时生效**，不用点应用
3. 不满意就点 **「恢复默认」**（当前项）或 **「全部视口」**（视口那一组 11 项一起恢复）
4. 5 套视口配色预设一键套用：暖灰 / 墨绿 / 深蓝 / 纯黑 / 浅灰
5. 满意后点 **「保存到偏好设置」**，否则重启会还原

细节说明：

* 覆盖 35 项颜色：视口背景上下、网格、线框、顶点、选中/活动物体、摄像机/空物体/灯光、骨骼、
  控件内部/边框/文字/条目、菜单、工具栏、分组框、饼菜单、已修改标记、图标着色、
  节点网格/背景/连线/选中节点、曲线编辑器与摄影表网格……
* **按版本自动过滤**：5.x 才有的 `user_interface.panel_*`（面板背景/标题/文字/描边）
  在 4.0 / 4.2 上不会出现，不需要你判断版本
* 主题颜色内部是 **8 位量化**存储的（写 0.5 读回 128/255 ≈ 0.50196），所以颜色会有极小偏差，这是 Blender 本身的存储方式
* RGB 类颜色项没有 alpha 通道，选择器里的 Alpha 会被忽略（面板上会提示）
* 只改颜色，不动主题里的其它设置，也不影响你的主题预设文件

### 面板顺序（拖动排序）

N 面板 ▸ `红红子lab` ▸ **「面板顺序」** 面板（它自己固定在最后）：

**本插件的面板**：按住每行左边的手柄上下拖，实时换位，松开确认、<kbd>Esc</kbd> 取消。
顺序存在插件偏好设置里，重启后仍然生效。

**侧栏标签页**：同一套拖动操作，用来排整个侧栏里各插件的标签页（Item / Tool / View /
红红子lab / 其它插件……）。旁边有 **「恢复默认标签页顺序」** 一键还原。

> **为什么不能直接按住标签页本身拖动**：侧栏那一列标签是 Blender 用 C 代码绘制和处理的
> （`panel_category_tabs_*`，源码里只有绘制与点击，没有任何重排逻辑），Python 拿不到标签栏的
> 鼠标事件，插件无法拦截"按住标签拖"这个动作 —— 社区里要排标签页也得另外装
> SIMPLE TABS 这类插件。所以这里的做法是：在面板里给出同一份标签页列表，
> 按住手柄拖动即可改顺序（效果等价，只是拖动发生在列表上）。

> 这些面板全部是**顶层面板**（没有用 `bl_parent_id`）：侧栏 N 面板不保证绘制子面板，
> Blender 自己的 N 面板脚本里一个子面板都没有，所以本插件一律用顶层面板，
> 并由测试静态守住这条（AST 检查类体里不许出现 `bl_parent_id`）。

原理与注意事项（都是实测/查源码得出的，不是猜的）：

* 顺序通过 Blender 官方的 **`bl_order`** 实现 —— 官方 RNA 定义写着
  “Panels with lower numbers are default ordered before panels with higher numbers”，
  而且官方提交明确说 `bl_order` 就是用来「不依赖注册顺序地指定面板先后」的，
  子面板排序也是同一个提交加上的
* `bl_order` 是**运行时写进面板类**的，所以插件每次启用/Blender 启动时都会按偏好设置重新应用一遍
* 标签页那块会**改写其它插件面板的 `bl_order`**：改动前会把原值记进偏好设置，
  「恢复默认标签页顺序」可以完整还原（包括类名与 `bl_idname` 不一致的插件）
* Blender 没有把「当前标签页顺序」暴露给 Python，所以标签页列表的初始次序只是按名字排的参考值，
  不代表界面上真实的先后 —— 按你想要的顺序拖即可
* 标签页顺序的**视觉效果**无法在无头环境自动化验证（Blender 不暴露这个 UI 状态）；
  顺序数据、`bl_order` 写入与还原都有自动化断言覆盖

### 改快捷键

**偏好设置 ▸ 插件 ▸ 红红子lab**：

* 点 **「按下新按键…」**，然后直接按你想用的键 → 立刻生效（<kbd>Esc</kbd> 取消、<kbd>Backspace</kbd> 恢复默认的 <kbd>Ctrl</kbd>+<kbd>E</kbd>）
* 或者手动改：勾 `Ctrl` / `Shift` / `Alt` / `Cmd`，再填「按键标识」（`E` 就是 E 键、`SPACE` 是空格、`ACCENT_GRAVE` 是 <kbd>`</kbd>）
* 「启用饼菜单快捷键」关掉后，只能用 N 面板呼出

也可以在 **偏好设置 ▸ 键位映射** 里搜索「红红子lab」，用 Blender 自带的方式改键 —— 两种方式是同一个键位。

---

## 三、每个功能的具体行为（边界情况都处理了）

### 删除所有材质

* 材质槽**一起删掉**，不只是把材质设为空
* **材质数据块本身保留**，不会顺手删除其它物体还在用的材质
* 物体级材质槽（`link == 'OBJECT'`）和数据块级材质槽都会清
* 网格 / 曲线等所有带材质的数据类型都支持；对空物体、灯光、摄像机等不会报错

### 应用所有修改器

* **逐个应用**，保持修改器顺序
* 在视口里被**禁用**的修改器：先临时启用再应用，报告里会说明有几个是这种情况
* **多用户（共享）数据**：自动复制成单用户再应用，不影响共享同一份网格的其它物体
* **带形态键的网格**：Blender 不允许逐个应用，自动改用「转换为网格」一次性烘焙（会丢形态键，无法避免）
* **被隐藏的物体**：临时取消隐藏、做完还原原状态
* **不在当前视图层的物体**（所在集合被排除等）：跳过并在报告里点名
* **曲线/文字上的构建类修改器**：Blender 本身就拒绝应用（提示要转网格），插件会明确报错并**保持原样**，不会偷偷把曲线改成网格
* **链接自其它 .blend 的物体**：跳过并提示

### 删除所有修改器

* 直接丢弃，**不烘焙**，几何不变
* 纯数据操作，不依赖可见性

三个操作都支持 <kbd>Ctrl+Z</kbd> 撤销。

---

## 四、兼容性与实测

**支持范围：Blender 4.0 及以上**（含 4.x 全系与 5.x）。

所有版本差异都收敛在 [redkolab/compat.py](redkolab/compat.py)：

* 给 `bpy.ops` 注入物体上下文统一用 `bpy.context.temp_override()`
  （4.0 已移除 2.80~3.1 时代的「字典覆盖」写法，所以旧分支已按需求删除）
* 插件偏好设置的 `bl_idname` 用 `__package__`：传统安装是 `redkolab`，
  4.2+ 扩展安装是 `bl_ext.<仓库>.redkolab`，写死字符串必挂
* 属性一律用**类型注解**写法（`key_type: StringProperty(...)`），
  赋值写法在 4.0+ 上根本不会生成 RNA 属性

### 实测矩阵

同一份 `redkolab-1.1.0.zip`，真机无头测试：真的装包、真启用、真建场景、真跑功能并断言。

| Blender | 安装入口 | 装成的模块 | 断言 | 结果 |
| --- | --- | --- | --- | --- |
| 4.0.2 | 插件页 Install from Disk | `redkolab` | 182 | ✔ |
| 4.2.23 LTS | 插件页 Install from Disk | `redkolab` | 182 | ✔ |
| 4.2.23 LTS | 扩展页 Install from Disk | `bl_ext.user_default.redkolab` | 180 | ✔ |
| 5.2.2 LTS | 插件页 Install from Disk | `redkolab` | 182 | ✔ |
| 5.2.2 LTS | 扩展页 Install from Disk | `bl_ext.user_default.redkolab` | 180 | ✔ |

> 断言数 182 / 180 的差别：扩展安装下 Blender 会接管元数据（不再暴露 `bl_info`），
> 测试会跳过 2 项元数据检查，功能断言一项不少。

扩展清单另外用官方校验器验过（4.2 与 5.2 都验）：

```bash
blender --factory-startup --command extension validate dist/redkolab-1.1.0.zip
# -> Success parsing TOML in "dist\redkolab-1.1.0.zip"
```

### 关于 2.80 ~ 3.x

按需求已不再支持（旧接口分支、旧安装格式都删掉了）。如果你以后要给 2.80~3.1 出包，
需要两处回退：

1. `compat.run_op()` 里补回 2.80~3.1 的字典覆盖分支（`temp_override` 是 3.2 才有的）
2. `__init__.py` 的 `bl_info["blender"]` 与清单的 `blender_version_min` 调回 `(2, 80, 0)`

---

## 五、目录结构

```
redkolab/
├─ redkolab/                    插件本体（这个目录就是插件，可直接拷进 scripts/addons/）
│  ├─ __init__.py               bl_info、注册/注销
│  ├─ blender_manifest.toml     4.2+ 扩展清单（版本号打包时自动同步）
│  ├─ compat.py                 跨版本兼容层（版本差异只在这里）
│  ├─ theme.py                  主题颜色（目标列表、读写、预设、版本容错）
│  ├─ panels.py                 N 面板顺序（分块顺序、标签页顺序、bl_order 与备份）
│  ├─ ops.py                    功能算符
│  └─ ui.py                     饼菜单、N 面板、偏好设置
├─ tools/
│  ├─ build_zips.py             打包（默认 nested 布局，可选其他布局做对比）
│  ├─ check_version.py          版本号一致性检查（发布守门）
│  ├─ inspect_zip.py            安装包结构自检（发布守门）
│  ├─ blender_test.py           Blender 内部真机测试（174 项断言，含边界情况）
│  ├─ run_tests.ps1             在指定 Blender 上跑测试（自动隔离配置目录）
│  ├─ release.ps1               一键发版：校验 → 升版本 → 本地部署 → 打 tag → 推送
│  ├─ deploy_local.ps1          把新包复制进本地插件 zip 库并替换旧版本
│  └─ probe.py                  版本差异探针（升级 Blender 前先跑它）
├─ .github/workflows/release.yml   自动构建 / 多版本真机测试 / 发布归档
└─ dist/                        本地打包产物（不入库，安装包归档在 Releases）
```

### 自己打包

```bash
python tools/build_zips.py              # 打正式包
python tools/build_zips.py --variant all  # 打所有候选布局，用于对比
```

### 自己跑测试

```powershell
powershell -ExecutionPolicy Bypass -File tools\run_tests.ps1 -Blender "C:\Program Files\Blender Foundation\Blender 4.5\blender.exe"
```

会自动判断安装方式（4.2+ 走扩展、4.0/4.1 走传统），并把 Blender 配置目录指到临时目录，
**不会污染你真实的 Blender 配置**。想单独验证某个安装入口：

```powershell
... -Mode extensions      # 强制走扩展页安装
... -Mode addon_install   # 强制走插件页安装
```

### 升级到更新的 Blender 时

```powershell
blender --background --factory-startup --python tools\probe.py
```

它会报告各版本底层行为的真实情况（`temp_override` 是否可用、注解式/赋值式属性哪种生效、
字典覆盖是否还能用、`keymap_items.new` 参数是否兼容），方便快速定位新版本的变化。

---

## 六、自动发布与归档

打一个 tag，就会自动完成「打包 → 自检 → 多版本真机测试 → 发布 → 归档」：

```powershell
powershell -ExecutionPolicy Bypass -File tools\release.ps1 -Bump patch
```

这条命令依次做：检查工作区干净 → 升版本号（`bl_info` 与 `blender_manifest.toml` 一起改，永远同步）
→ 版本一致性检查 → 打包 → 安装包结构自检 → **复制进本地插件 zip 库并替换旧版本**
→ 提交 → 打 `v1.4.0` tag → 推送（带重试）。
任何一步不过就直接中止，不会把坏包推上去。加 `-DryRun` 可以只看检查结果不推送。

**本地插件 zip 库**默认是 `D:\build\Blender\4.0\4.0chajian`（放 Boxcutter / HardOps / MACHIN3tools
这类安装包的地方）：每次发版会把新的 `redkolab-<版本>.zip` 复制进去，并删掉里面的旧版本
（只删该目录下匹配 `redkolab-*.zip` 的文件，别的插件包与文件夹一律不碰）。

**已安装副本**默认是 `%APPDATA%\Blender Foundation\Blender\4.0\scripts\addons`：每次发版会把
`redkolab\` 整个替换成新版本的内容，这样 Blender 下次启动加载的就是最新的
（**装完记得重启 Blender**，或停用再启用插件）。想换目录用 `-DeployTo` / `-InstallTo`，
不想动本地用 `-NoDeploy` / `-NoInstall` 跳过，想留历史 zip 用 `-KeepOld`：

```powershell
powershell -ExecutionPolicy Bypass -File tools\deploy_local.ps1 -Target "D:\其它\插件库" -InstallTo "D:\其它\addons"
```

推送后 [.github/workflows/release.yml](.github/workflows/release.yml) 自动执行：

| 阶段 | 内容 |
| --- | --- |
| 打包自检 | tag 与源码版本一致性、打包、结构自检（布局 / 元数据 / 属性写法 / 上下文覆盖）、生成 SHA256SUMS.txt |
| 真机测试 | 下载 Blender **4.2 / 4.5 / 5.2**（首次下载后走缓存），在隔离的配置目录里真装真跑 90+ 项断言；5.2 上再额外走一遍扩展页安装入口 |
| 发布归档 | 建立 Release，把 `redkolab-<版本>.zip` 与 `SHA256SUMS.txt` 作为附件永久归档 |

**任何一步失败都不会发布** —— 不会出现版本号对不上的包或装不上的包被归档。

只想构建和测试、不发版：仓库页 ▸ Actions ▸ Release ▸ Run workflow（手动触发不发 Release）。

---

## 七、已知限制

* 编辑模式下按钮是灰的（Blender 不允许在编辑模式改动材质/修改器数据），面板会提示切回物体模式
* 饼菜单的「按下新按键…」捕获需要真实窗口，无法在无头环境自动化测试，其余功能都有自动化覆盖
* 曲线的构建类修改器无法应用（Blender 的限制，见上文）
* 「应用所有修改器」遇到形态键会丢失形态键（转换烘焙的固有代价）
* 主题颜色按 8 位量化存储，写入值读回会有约 1/255 的偏差（Blender 本身的存储方式）
* RGB 类主题颜色项没有 alpha 通道，颜色选择器里的 Alpha 会被忽略
* 主题改动立即生效，但想重启后保留需要点「保存到偏好设置」（或正常退出 Blender 让偏好自动保存）
* 5.x 独有的 `user_interface.panel_*` 颜色项在 4.0 / 4.2 上会自动隐藏（不是 bug）
* 面板顺序的**拖动手感**与标签页排序的**视觉结果**无法在无头环境自动化验证；
  顺序数据、`bl_order` 写入、备份与还原都有自动化断言覆盖
* 标签页排序会改写其它插件面板的 `bl_order`（可一键还原）；若某个插件之后自行重新注册面板，
  它的顺序可能被重置，重新拖一次即可
* 默认快捷键 <kbd>Ctrl</kbd>+<kbd>E</kbd> 在**编辑模式**下会与 Blender 的「边」菜单冲突（Blender 优先），
  所以饼菜单在编辑模式下不弹出；偏好设置里可以随时换成别的键
* **侧栏的标签页本身没法用鼠标拖动排序**：那部分是 Blender 的 C 代码绘制并处理鼠标的，
  Python 没有钩子（社区里排标签页也要另装 SIMPLE TABS 之类插件）。本插件的做法是在
  「面板顺序」面板里给一份标签页列表，拖动列表即改顺序
* 许可证：GPL-3.0-or-later（Blender 插件的通行许可）
