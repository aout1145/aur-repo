# AUR 自动构建仓库

> 🌐 在线仓库主页：<https://aur.aout.top>

基于 GitHub Actions 的 AUR 包自动构建与发布系统：把 AUR 包编译成二进制包，发布到
**GitHub Releases**，生成可以直接加入 `pacman` 的软件仓库；可选通过 **Cloudflare Worker**
加速（对访问 GitHub 较慢的网络尤其有用）。

---

## 目录

- [特性](#特性)
- [快速开始](#快速开始)
- [部署步骤](#部署步骤)
- [Cloudflare 加速](#cloudflare-加速)
- [GPG 签名](#gpg-签名可选)
- [工作原理](#工作原理)
- [目录结构](#目录结构)
- [本地测试](#本地测试)
- [故障排查](#故障排查)
- [安全提示](#安全提示)

---

## 特性

- **配置简单**：根目录一个 [`packages.toml`](packages.toml) 决定要构建哪些包。
- **正确处理 AUR 依赖**：自动解析依赖图（含只存在于 AUR 的依赖），按拓扑顺序先构建依赖再构建目标包。
- **按需更新**：只有「仓库里没有」或「AUR 版本比已发布的更新」时才重建；定时任务也只检查版本，
  **不会无条件重新构建**。
- **按包原子更新**：单个包构建失败不会中止整体，失败包及其依赖分量会被「锁定」，其余正常更新。
- **无需额外存储**：产物直接作为本仓库某个 Release 的附件，不依赖 R2/对象存储。
- **规避 GitHub 文件名限制**：自动重命名含 `:`、`+` 等字符的包文件，并保证数据库里的
  `%FILENAME%` 与附件名一致。
- **Cloudflare 加速 + 主页**：附带一个 Worker，把 Release 附件代理到 Cloudflare 边缘并缓存，
  同时在根路径渲染一个仓库主页。
- **可签名**：支持用 GPG 对软件包和数据库签名，并自动发布公钥。

---

## 快速开始

> 下例中的 `OWNER/REPO`、`repo.example.com`、`custom` 等是占位符，换成你自己的即可。

1. **加包**：编辑 [`packages.toml`](packages.toml)，把要构建的 AUR 包名填进 `packages`。
2. **构建**：进入 **Actions → Build AUR repository → Run workflow**（或直接 push 改动）；
   产物自动发布到 Release。
3. **接入**：在客户端 `pacman.conf` 里加一段（见 [部署步骤](#部署步骤)）。
4. **（可选）加速**：部署 Cloudflare Worker，用 `https://repo.example.com` 作为 `Server`。

---

## 部署步骤

### 1. 准备

- 准备好一个 GitHub 仓库（本系统就在本仓库内运行）。
- 不需要任何额外存储密钥：workflow 使用自动提供的 `GITHUB_TOKEN`
  （`permissions: contents: write`）创建 Release 并上传附件。
- 想要签名的话，见 [GPG 签名](#gpg-签名可选)。

### 2. 配置 `packages.toml`

只有 `packages` 里的包是「目标」，它们的 AUR 依赖会被递归解析、按序构建并一并发布。

```toml
packages = [
  "paru",
  "aurutils",
  # "mpv-full-git",      # 示例：会自动带上 AUR 依赖 ffmpeg-git
]

[repo]
name = "custom"          # 数据库 -> custom.db，客户端配置段名 [custom]
tag  = "repo"            # 所有产物放在这个 tag 的 Release 里
arch = "x86_64"
remove_old = false       # 重建/删除包后清理旧附件（默认 false）

[build]
run_checks = false          # 是否执行 check()（会拉取 checkdepends）
rebuild_dependents = false  # 依赖被重建时，是否连带重建依赖它的包
update_vcs = false          # 是否每次重建 VCS 包（-git 等）
skip_pgp_check = false      # 构建时跳过源码 PGP 校验
```

单包覆盖（键名必须与 `packages` 里完全一致）：

```toml
[overrides.some-daemon-git]
vcs = true          # 强制按 VCS 包处理
update_vcs = true   # 每次运行都重建
skip_check = true   # 不跑 check()
```

### 3. 触发流水线

| 触发方式 | 行为 |
| --- | --- |
| 手动 `Run workflow` | 可只构建部分包（`packages`）、可 `force` 强制重建、可 `update_vcs` 重建 VCS 包。 |
| 推送 `packages.toml` / 脚本 / workflow | 按版本比较结果构建。 |
| 定时（默认每天 03:17 UTC） | 仅检查版本，只重建真正过期的包；可改/删 `schedule`。 |

### 4. 客户端接入

在 `/etc/pacman.conf` 末尾加一段。`Server` 用 **Cloudflare（推荐）**：

```ini
[custom]
SigLevel = Required DatabaseOptional
Server = https://repo.example.com
```

或者直连 GitHub Release（无需 Worker）：

```ini
[custom]
SigLevel = Required DatabaseOptional
Server = https://github.com/OWNER/REPO/releases/download/repo
```

**已签名**时先导入公钥（公钥随仓库发布为 `repo.gpg`）：

```bash
curl -fsSL https://repo.example.com/repo.gpg -o /tmp/repo.gpg
sudo pacman-key --add /tmp/repo.gpg
sudo pacman-key --lsign-key <你的GPG_KEYID>
```

**未签名**时把 `SigLevel` 改成 `Optional TrustAll`。然后：

```bash
sudo pacman -Syu
sudo pacman -S <包名>
```

---

## Cloudflare 加速

`cloudflare/` 里是一个 Worker：把 `/<文件>` 映射到
`https://github.com/OWNER/REPO/releases/download/repo/<文件>`，跟随 GitHub 重定向并把结果
缓存在 Cloudflare 边缘。包文件**不可变**，缓存 30 天并带 `Cache-Tag`（便于按标签清理，见
[缓存与旧文件清理](#缓存与旧文件清理)）；数据库和公钥不缓存，始终回源。

> **这不是开放代理。** Worker 只放行：数据库 `<DB_NAME>` 及其签名、包文件
> `*.pkg.tar.<ext>` 及其签名、公钥 `repo.gpg`；其它一律 `404`，且拒绝含 `/` 或路径穿越的请求。
> 它也不会透传 GitHub 的页面：文件不存在时返回自己的纯文本 `404`，上游 4xx/5xx 返回 `502`，
> 并剥离 `x-github-*`、`x-fastly-*`、`via`、`server` 等来源标识头。

### 部署 Worker

**方式一：本地 wrangler**

```bash
cd cloudflare
# 编辑 wrangler.toml：GITHUB_REPO / RELEASE_TAG / DB_NAME 与 packages.toml 对应
npx wrangler deploy
```

输出形如 `https://aur-repo.<子域>.workers.dev`，把它（或自定义域名）作为 pacman 的 `Server`。

**方式二：GitHub Actions（推荐）**

1. 在 Cloudflare 建一个 API Token，权限：
   - **Account → Workers Scripts → Edit**（上传 Worker，必需）
   - **Zone → Workers Routes → Edit**（绑定自定义域名时需要），Zone Resources 包含目标域名。
   - **Zone → Cache Purge → Purge**（清理旧的包缓存时需要）。
   > 直接用官方的 **“Edit Cloudflare Workers”** Token 模板最省事（再补上 Cache Purge）。
2. 仓库 Secrets 添加：
   - `CLOUDFLARE_API_TOKEN`
   - `CLOUDFLARE_ACCOUNT_ID`（必须是**拥有该 zone 的账户**）
   - `CLOUDFLARE_ZONE_ID`（清理缓存用，可在 Cloudflare 域名概览页看到）
3. 修改 `cloudflare/` 下的文件并 push，或手动运行
   **Actions → Deploy Cloudflare repository proxy**。
   自动部署由 `cloudflare/wrangler.toml` 的 `[deploy] auto`（默认 `true`）控制；设为 `false`
   时 deploy job 会被跳过。

### 缓存与旧文件清理

包文件是不可变的，所以在边缘缓存（每个数据中心各自保存）——**GitHub 上删掉附件并不会让 Cloudflare 的缓存消失**。
为了避免旧版本/已删除的包一直占着缓存，采取了双保险：

- **有限 TTL**：包文件只缓存 30 天（`PACKAGE_TTL_SECONDS`），到期自动失效，属于兜底。
- **按标签精确清理**：Worker 缓存包文件时会写入 `Cache-Tag: pkg,pkg:<文件名>`；
  `publish.py` 在发布时算出「已失效文件」（上一次 `repo.json` 的文件名 − 当前数据库文件名），
  调用 Cloudflare `purge_cache` 按 tag 清理。
  - 由 `cloudflare/wrangler.toml` 的 `[deploy] purge`（默认 `true`）控制；
  - 需要 `CLOUDFLARE_ZONE_ID` 与带 **Zone → Cache Purge → Purge** 权限的 `CLOUDFLARE_API_TOKEN`；
  - 这是 **zone 级** 操作，只在用自定义域名时可用（`*.workers.dev` 无法清理）。

### 自定义域名（可选）

在 `cloudflare/wrangler.toml` 中启用：

```toml
workers_dev = false

[[routes]]
pattern = "aur.example.com"
custom_domain = true
```

要求该域名（zone）在同一 Cloudflare 账户下，且 Token 具备 **Zone → Workers Routes → Edit**。

### 主页

访问 <https://repo.example.com>（或你自己的域名）会看到一个极简主页（大标题取自 `wrangler.toml` 的 `TITLE`）：

- 如何把仓库加入 `pacman.conf`（段名由 `DB_NAME` 推导，`Server` 用当前域名）；
- 是否启用签名：已签名时给出 `repo.gpg` 导入与 `pacman-key --lsign-key` 步骤，否则 `Optional TrustAll`；
- **无表头的两列表格**：
  - 第一列：完整包文件名，超链接直达 `https://<域名>/<filename>`（未发布过的包显示包名，无链接）；
  - 第二列：该包的**最后更新时间 + 状态**（`正常` / `失败` / `已锁定`），时间格式 `YYYY-MM-DD HH:MM UTC`。

主页数据来自同一 Release 下的 `repo.json`（由 `publish.py` 生成）：
包列表、每包 `updated_at`/`status`、`signed`、`key_id`、生成时间。
`status` 取值：`ok`、`failed`（构建失败）、`blocked`（自身没失败，但因共享依赖被连带锁定）。

> 若只需要 GitHub 直连、不要加速，可完全忽略 Cloudflare，删除 `cloudflare/` 与
> `.github/workflows/deploy-worker.yml` 即可。

---

## GPG 签名（可选）

```bash
# 1. 生成签名密钥
gpg --batch --passphrase '' --quick-generate-key "My AUR Repo <me@example.com>" rsa4096 sign never
gpg --list-secret-keys --keyid-format=long     # 记下 KEYID

# 2. 导出私钥
gpg --armor --export-secret-keys <KEYID>
```

仓库 Secrets 添加：

| 名称 | 说明 |
| --- | --- |
| `GPG_KEY` | 上面的 KEYID |
| `GPG_PRIVATE_KEY` | ASCII armor 的私钥内容 |
| `GPG_PASSPHRASE` | 私钥口令（无口令可留空） |

CI 会用该密钥对每个软件包和数据库做分离签名，并发布 `repo.gpg` 供客户端导入。

---

## 工作原理

### 流水线

每次运行分成三个 job：

| Job | 作用 |
| --- | --- |
| `plan` | 从 Release 下载已发布的数据库，解析 `packages.toml`（含递归 AUR 依赖）的版本，和已发布版本比较，决定要构建 / 移除哪些包，输出 `plan.json`。 |
| `build` | 按依赖顺序构建（`makepkg`），重命名包文件、在本地组装数据库，并把结果作为 Actions 工件保存。**此阶段不接触 Release。** |
| `publish` | 只上传「成功且未被锁」的包，生成 `repo.json`，最后替换数据库（失败会回滚）。 |

### 按包 / 依赖分量的原子更新

- `build.py` 逐个构建，**某个包失败不会中止整体**：记录失败并继续，依赖它的包标记为跳过。
- 把依赖图看作**无向图**，任何失败节点所在的**整个连通分量**都会被「锁住」：
  - 失败包本身；
  - 它的依赖（向下）；
  - 与它共享依赖的所有包（向上）。
- 被锁的包：**不发布新版本、不更新数据库条目、不删除旧文件**；未锁定的成功包照常上传并更新数据库。
- 发布顺序：先传包和 `repo.gpg`，最后替换 `<name>.db`（替换失败回滚）。
- 新加的包第一次构建就失败（没有上一次状态）时，`repo.json` 里记一条 `status: "failed"`、无文件名和
  链接，主页照常显示为「失败」。

### 依赖处理

`scripts/aur_lib.py` 会：

1. 从 AUR 拉取每个包（`pkgbase`）的 `.SRCINFO`；
2. 收集 `depends` / `makedepends` /（可选）`checkdepends`；
3. 判断依赖是否来自官方仓库（读取 `/var/lib/pacman/sync/*.db`，含 `provides`）；
4. 官方仓库没有的依赖，通过 AUR RPC（按包名、再按 `provides`）找到对应 `pkgbase`，加入依赖图；
5. 拓扑排序得到构建顺序；
6. 依赖图中的每个包都会被发布，使仓库自包含，下次运行也能判断依赖是否已最新。

`build` job 在**临时 Arch 容器**里构建，用 `makepkg -s` 安装依赖，并把**被其它包依赖**的已构建包
`pacman -U` 安装进去供后续使用。只有真正作为依赖的包会被装进容器，因此像 `cpeditor` 与
`cpeditor-bin` 这种互相冲突但无需同时安装的包可以安全共存于同一仓库。

### 文件名重命名

GitHub Release 附件名只允许字母、数字和 `. - _`。Arch 包文件名带 `epoch` 时含 `:`，`pkgver`
可能含 `+`，直接上传会被 GitHub 改写，导致数据库里的 `%FILENAME%` 与实际附件名不一致而 404。
因此 `build.py` 会在 `repo-add` **之前**把包文件重命名为安全名字（非法字符换成 `_`），保证
`%FILENAME%` 与附件名一致；包内部元数据不变，版本号仍保留 epoch。

### 按需更新逻辑

对每个包，`plan.py` 采用如下判断（`force` 优先）：

- 未在仓库中 → 构建；
- AUR 版本 > 已发布版本（`vercmp`）→ 构建；
- VCS 包（源码 URL 形如 `git+…`）→ 默认不因版本比较而构建，只有开启 `update_vcs` 或 `force` 才构建；
- 可选 `rebuild_dependents = true`：某个 AUR 依赖本次被重建时，一并重建依赖它的包。

已发布版本来自 Release 上的 `<name>.db`，运行器是「无状态」的，不需要往仓库 commit 任何东西。

### 删除包

仓库按 `packages.toml` 声明同步：删除某个包（或某依赖不再被任何包需要）后，下次运行会把它从
数据库移除。为避免依赖解析失败时误删，只有整张依赖图都解析成功才执行移除。旧附件是否删除取决于
`remove_old`。

---

## 目录结构

```
.
├── packages.toml                 # 要构建的 AUR 包列表 + 全局/单包配置
├── .github/workflows/
│   ├── build.yml                 # plan / build / publish 流水线
│   └── deploy-worker.yml         # 部署 Cloudflare Worker（改 cloudflare/ 自动触发，可手动）
├── cloudflare/
│   ├── worker.js                 # 代理 + 缓存 + 主页
│   └── wrangler.toml             # Worker 配置（含 [deploy] auto 开关）
└── scripts/
    ├── aur_lib.py                # AUR 客户端、依赖解析、数据库读取、版本比较、文件名清洗
    ├── plan.py                   # 生成构建计划（决定重建哪些包）
    ├── build.py                  # 按依赖顺序构建、锁定失败分量、组装本地仓库
    └── publish.py                # 上传成功且未锁定的包，生成 repo.json（可选清理旧附件）
```

---

## 本地测试

脚本只依赖 Python 标准库、`pacman`、`vercmp`、`bsdtar`（`libarchive`）、`git`、`gh`。

```bash
# 只生成构建计划
python3 scripts/plan.py --config packages.toml --out plan.json

# 演练构建（不真正执行 makepkg / gh）
python3 scripts/build.py --config packages.toml --plan plan.json --dry-run

# 演练上传（未登录 gh 时会跳过）
python3 scripts/publish.py --config packages.toml --repo-dir public --dry-run
```

指定仓库、只构建某个包：

```bash
python3 scripts/plan.py --config packages.toml --repo OWNER/REPO --packages paru --out plan.json
```

---

## 故障排查

- **Release 附件名被改写 / pacman 404**：系统已在 `repo-add` 前重命名文件；若手动往 Release 里放了
  带 `:`、`+` 的文件，请去掉这些字符。
- **`plan` 有依赖无法解析**：该依赖可能来自非官方仓库（如 `archlinuxcn`）。官方仓库没有、AUR 也没有
  的依赖无法构建，需要手动处理。
- **`makepkg` 以 root 失败**：workflow 会创建 `builder` 用户运行 `makepkg`；本地运行 `build.py` 时
  请不要用 root。
- **首次运行没有数据库**：正常，`plan` 会把所有配置的包标记为「未发布」并构建。
- **Cloudflare 缓存**：只有包文件会被边缘缓存（30 天，并带 `Cache-Tag`）；数据库与公钥始终回源，
  不会出现数据库过期或与签名不匹配。旧版本/已删除的包会在发布时按标签清理。
- **缓存清理报权限错误**：`CLOUDFLARE_API_TOKEN` 缺少 **Zone → Cache Purge → Purge**，或没设
  `CLOUDFLARE_ZONE_ID`；也可以把 `cloudflare/wrangler.toml` 的 `[deploy] purge` 设为 `false` 关闭。
- **部署 Worker 报 `No access to the specified resource (/zones/<id>/workers/routes)`**：API Token
  缺少 **Zone → Workers Routes → Edit**，或该域名不在 `CLOUDFLARE_ACCOUNT_ID` 账户下。补齐后重跑；
  或先把 `workers_dev = true` 并去掉 `[[routes]]`，用 `*.workers.dev` 地址。
- **附件超过 2 GB**：GitHub Release 单附件上限 2 GB，超大包需改用其它存储。
- **`remove_old`**：删除不再被数据库引用的旧附件，默认关闭。

---

## 安全提示

构建过程会在 CI 中执行 AUR 上的 `PKGBUILD`。AUR 包由社区维护，请只构建你信任的包，并留意
`packages.toml` 的变更。`makepkg` 以非 root 的 `builder` 用户运行，且不会继承 `GH_TOKEN`，
因此 `PKGBUILD` 无法直接读取发布用的令牌；但请仍然只构建可信来源的包。
