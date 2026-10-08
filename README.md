# AUR 自动构建仓库

基于 GitHub Actions 的 AUR 包自动构建与发布系统：把 AUR 包编译成二进制包，发布到
**GitHub Releases**，并生成可以直接添加进 `pacman` 的软件仓库；可选地通过
**Cloudflare Worker** 加速（对访问 GitHub 较慢的网络尤其有用）。

- **配置简单**：根目录一个 [`packages.toml`](packages.toml) 决定要构建哪些包。
- **正确处理 AUR 依赖**：自动解析依赖图（包括只存在于 AUR 的依赖），按拓扑顺序先构建
  依赖再构建目标包。
- **按需更新**：只有「仓库里没有」或「AUR 版本比已发布的更新」时才会重建；定时任务也只
  是检查版本，**不会无条件重新构建**。
- **无需额外存储**：产物直接作为本仓库某个 Release 的附件，不依赖 R2/对象存储。
- **规避 GitHub 文件名限制**：自动重命名包含 `:`、`+` 等字符的包文件，并让数据库中的
  `%FILENAME%` 与实际附件名保持一致。
- **Cloudflare 加速**：附带一个 Worker，把 GitHub Release 附件代理到 Cloudflare 边缘并缓存。
- **可签名**：支持用 GPG 对软件包和数据库签名，并自动发布公钥。

---

## 工作原理

每次运行分成三个 job：

| Job | 作用 |
| --- | --- |
| `plan` | 从 Release 下载已发布的仓库数据库，解析 `packages.toml` 中所有包（含递归 AUR 依赖）的版本，和已发布版本比较，决定要构建 / 移除哪些包，输出 `plan.json`。 |
| `build` | 按依赖顺序构建（`makepkg`），把构建出的包安装进容器供后续依赖使用，重命名包文件、在本地组装仓库数据库，并把结果作为 Actions 工件保存。**此阶段不接触 Release。** |
| `publish` | 依赖 `build`，只有在 **build job 全部成功** 后才运行；把包先上传、数据库最后替换（替换失败会回滚）。 |

### 原子更新

- 任何包构建失败 → `build` job 失败 → `publish` job 不会启动，**GitHub Release 完全不变**。
- `publish` 内部也按安全顺序执行：先上传所有包文件和 `repo.gpg`，最后才替换 `<name>.db` 及其签名。
  即使上传中途失败，数据库仍指向真实存在的包；替换数据库失败时脚本会用事先下载的旧数据库回滚。
- 因此要么整体更新成功，要么仓库保持一致状态，不会出现「数据库引用了不存在的包」。

**触发方式（都不会无条件重建）：**

| 触发 | 行为 |
| --- | --- |
| 手动 `Run workflow` | 可只构建部分包、可 `force` 强制重建、可 `update_vcs` 重建 VCS 包。 |
| 推送 `packages.toml` / 脚本 / workflow | 按版本比较结果构建。 |
| 定时（默认每天 03:17 UTC） | 仅检查版本；只重建真正过期的包。可在 workflow 中删除或调整 `schedule`。 |

### 依赖处理

`scripts/aur_lib.py` 会：

1. 从 AUR 拉取每个包（`pkgbase`）的 `.SRCINFO`；
2. 收集 `depends` / `makedepends` /（可选）`checkdepends`；
3. 判断依赖是否来自官方仓库（直接读取 `/var/lib/pacman/sync/*.db`，包含 `provides`）；
4. 官方仓库没有的依赖，通过 AUR RPC（按包名、再按 `provides`）找到对应的 `pkgbase`，加入依赖图；
5. 对依赖图做拓扑排序，得到构建顺序；
6. 依赖图中的每个包都会被发布，这样仓库是自包含的，下次运行也能知道依赖是否已是最新，而不会重复构建。

`build` job 在**临时的 Arch 容器**里按顺序构建，用 `makepkg -s` 安装依赖，并把刚构建的包
`pacman -U` 安装进去，所以依赖能被后续目标包正确使用。

### 文件名为什么会被重命名

GitHub Release 的附件名只允许字母、数字和 `. - _`，其它字符会被 GitHub 自动改写。
Arch 包文件名在带 `epoch` 时会包含 `:`（例如 `foo-1:2.0-1-x86_64.pkg.tar.zst`），
`pkgver` 也可能包含 `+`。

如果直接上传，附件名会变成 `foo-1_2.0-1-...`，而仓库数据库里的 `%FILENAME%` 仍是原文，
导致 pacman 下载 404。因此 `build.py` 会在 `repo-add` **之前**把包文件重命名为
GitHub 安全的名字（把非法字符换成 `_`），这样数据库里的 `%FILENAME%` 与附件名一致，
pacman 就能正确下载。包内部元数据不受影响，版本号里仍然保留 epoch。

### 按需更新逻辑

对每个包，`plan.py` 采用如下判断（`force` 优先）：

- 未在仓库中 → 构建；
- AUR 版本 > 已发布版本（`vercmp`）→ 构建；
- VCS 包（源码 URL 形如 `git+…`）→ 默认不因版本比较而构建，只有开启 `update_vcs` 或 `force` 才构建；
- 可选 `rebuild_dependents = true`：当某个 AUR 依赖本次被重建时，一并重建依赖它的包。

已发布版本来自 Release 上的 `<repo>.db`，因此运行器是「无状态」的，不需要在仓库里 commit 任何东西。

### 删除包

仓库是「按 `packages.toml` 声明同步」的：从 `packages.toml` 删除某个包（或某个依赖不再被任何包
需要）后，下次运行会自动把它的条目从数据库里移除，`pacman` 就不再看到它。为避免在依赖解析失败
时误删，只有在整张依赖图都解析成功时才会执行移除。

被移除包的旧附件是否删除取决于 `remove_old`：开启后会一并清理，否则附件保留在 Release 里但不再
被数据库引用（不影响 pacman）。

---

## 快速开始

### 1. 编辑 `packages.toml`

```toml
packages = [
  "paru",
  "aurutils",
  "mpv-full-git",   # 会自动带上 AUR 依赖 ffmpeg-git
]

[repo]
name = "custom"   # 数据库 -> custom.db，客户端配置 [custom]
tag  = "repo"     # 所有产物放在这个 tag 的 Release 里
```

可用的全局/单包选项：

```toml
[build]
run_checks = false          # 是否执行 check()（会拉取 checkdepends）
rebuild_dependents = false  # 依赖被重建时是否连带重建依赖它的包
update_vcs = false          # 是否每次重建 VCS 包
skip_pgp_check = false      # 构建时跳过源码 PGP 校验

[overrides.some-daemon-git]
vcs = true
update_vcs = true
skip_check = true
```

### 2. 运行

- 进入 **Actions → Build AUR repository → Run workflow**：
  - `packages`：只构建其中的包（空格或逗号分隔，留空表示全部）；
  - `force`：即使已是最新也重建；
  - `update_vcs`：同时重建 `-git` 等 VCS 包。
- 或直接 push 修改到 `packages.toml`。
- 定时任务按需自动更新。

workflow 使用自动提供的 `GITHUB_TOKEN`（`permissions: contents: write`）创建 Release 并上传附件，
**不需要额外配置任何存储密钥**。

### 3. 客户端使用

#### 方式 A：直连 GitHub Release

```ini
[custom]
SigLevel = Required DatabaseOptional
Server = https://github.com/OWNER/REPO/releases/download/repo
```

（`OWNER/REPO` 换成你的仓库，`repo` 是 `packages.toml` 里的 `tag`。pacman 会自动跟随
GitHub 的下载重定向。）

#### 方式 B：通过 Cloudflare 加速（推荐）

见下一节。使用 Worker 地址：

```ini
[custom]
SigLevel = Required DatabaseOptional
Server = https://aur-repo.<你的子域>.workers.dev
```

**已签名**时，先导入公钥（公钥随仓库发布为 `repo.gpg`）：

```bash
curl -fsSL <Server>/repo.gpg -o /tmp/repo.gpg
sudo pacman-key --add /tmp/repo.gpg
sudo pacman-key --lsign-key <你的GPG_KEYID>
```

**未签名**时把 `SigLevel` 改为 `Optional TrustAll`。

然后：

```bash
sudo pacman -Syu
sudo pacman -S <包名>
```

---

## Cloudflare 加速

`cloudflare/` 目录里是一个 Worker：把 `/<文件>` 映射到
`https://github.com/OWNER/REPO/releases/download/<tag>/<文件>`，跟随 GitHub 的重定向并把结果
缓存在 Cloudflare 边缘。包文件是**不可变**的，缓存一年；数据库和公钥不缓存，始终回源，
避免数据库与签名不一致。

> **这不是开放代理。** Worker 只允许下列路径，其它一律返回 `404`，避免被用作任意内容的
> 代理（网络钓鱼等）：
> - 仓库数据库 `<DB_NAME>` 及其签名 `<DB_NAME>.sig`（`DB_NAME` 需与 `packages.toml` 的
>   `[repo].name` + `.db` 一致，例如 `aout.db`）
> - 包文件 `*.pkg.tar.<ext>` 及其签名 `*.pkg.tar.<ext>.sig`
> - 公钥 `repo.gpg`
>
> 只允许平铺的文件名，含 `/` 或路径穿越的请求会被拒绝。

### 部署方式一：本地 wrangler

```bash
cd cloudflare
# 编辑 wrangler.toml：GITHUB_REPO 改成你的 owner/repo，RELEASE_TAG 与
# packages.toml 的 [repo].tag 一致，DB_NAME 与 [repo].name + ".db" 一致
npx wrangler deploy
```

输出形如 `https://aur-repo.<子域>.workers.dev`，把它作为 pacman 的 `Server`。

### 部署方式二：GitHub Actions

1. 在 Cloudflare 创建 API Token 和 Account ID。Token 权限：
   - **Account → Workers Scripts → Edit**（上传 Worker，必需）
   - **Zone → Workers Routes → Edit**（绑定自定义域名/路由时需要），并把
     Zone Resources 设为包含目标域名所在的 zone。只用 `*.workers.dev` 时不需要。
2. 仓库 Secrets 添加 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`。
   注意 `CLOUDFLARE_ACCOUNT_ID` 必须是**拥有该 zone 的账户**。
3. 手动运行 **Actions → Deploy Cloudflare repository proxy**。
   （该 workflow 会自动把 `GITHUB_REPO` 注入为当前仓库。）

> 最省事的方式是直接用 Cloudflare 的 **“Edit Cloudflare Workers”** Token 模板创建，
> 并确认它包含上面的 Zone 权限。

### 绑定自定义域名（可选）

在 `cloudflare/wrangler.toml` 中启用：

```toml
workers_dev = false

[[routes]]
pattern = "aur.example.com"
custom_domain = true
```

要求：该域名（zone）在**同一个 Cloudflare 账户**下，且 API Token 具备
**Zone → Workers Routes → Edit**。再次部署后即可用 `https://aur.example.com` 作为 `Server`。

> 若只需要 GitHub 直连、不需要加速，可以完全忽略 Cloudflare，删除 `cloudflare/` 与
> `deploy-worker.yml` 即可。

---

## 可选：配置 GPG 签名

```bash
# 1. 生成一个签名密钥
gpg --batch --passphrase '' --quick-generate-key "My AUR Repo <me@example.com>" rsa4096 sign never
gpg --list-secret-keys --keyid-format=long     # 记下 KEYID

# 2. 导出私钥（无口令时直接导出即可）
gpg --armor --export-secret-keys <KEYID>
```

在仓库 Secrets 中添加：

| 名称 | 说明 |
| --- | --- |
| `GPG_KEY` | 上面的 KEYID |
| `GPG_PRIVATE_KEY` | ASCII armor 的私钥内容 |
| `GPG_PASSPHRASE` | 私钥口令（无口令则留空） |

CI 会用该密钥对每个软件包和数据库做分离签名，并发布 `repo.gpg` 供客户端导入。

---

## 目录结构

```
.
├── packages.toml                 # 要构建的 AUR 包列表 + 全局/单包配置
├── .github/workflows/
│   ├── build.yml                 # plan / build / publish 流水线
│   └── deploy-worker.yml         # 部署 Cloudflare Worker（可选，手动）
├── cloudflare/
│   ├── worker.js                 # Cloudflare 代理 + 缓存
│   └── wrangler.toml
└── scripts/
    ├── aur_lib.py                # AUR 客户端、依赖解析、仓库数据库读取、版本比较、文件名清洗
    ├── plan.py                   # 生成构建计划（决定重建哪些包）
    ├── build.py                  # 按依赖顺序构建并组装本地仓库
    └── publish.py                # 上传到 GitHub Release（可选清理旧附件）
```

---

## 本地测试

脚本只依赖 Python 标准库、`pacman`、`vercmp`、`bsdtar`（`libarchive`）、`git`、`gh`。

```bash
# 只生成构建计划，不构建
python3 scripts/plan.py --config packages.toml --out plan.json

# 演练构建流程（不真正执行 makepkg / gh）
python3 scripts/build.py --config packages.toml --plan plan.json --dry-run

# 演练上传（未登录 gh 时会直接跳过）
python3 scripts/publish.py --config packages.toml --repo-dir public --dry-run
```

指定仓库、只构建某个包：

```bash
python3 scripts/plan.py --config packages.toml --repo OWNER/REPO --packages paru --out plan.json
```

---

## 故障排查

- **Release 附件名被改写 / pacman 404**：本系统已在 `repo-add` 前重命名文件，保证数据库里的
  `%FILENAME%` 与附件名一致；如果手动往 Release 里放了带 `:`、`+` 的文件，请去掉这些字符。
- **`plan` 显示某些依赖无法解析**：该依赖可能来自非官方仓库（如 `archlinuxcn`）。官方仓库没有
  且 AUR 也没有的依赖无法构建，需要手动处理。
- **`makepkg` 在容器里以 root 失败**：workflow 会创建 `builder` 用户并让 `makepkg` 以该用户运行；
  本地运行 `build.py` 时请不要用 root。
- **首次运行没有数据库**：属于正常情况，`plan` 会把所有配置的包标记为「未发布」并构建。
- **Cloudflare 缓存**：只有包文件（不可变）会被边缘缓存；数据库与公钥始终回源，因此不会出现数据库过期或与签名不匹配的问题。
- **部署 Worker 报 `No access to the specified resource (/zones/<id>/workers/routes)`**：API Token 缺少 **Zone → Workers Routes → Edit**，或该域名（zone）不在 `CLOUDFLARE_ACCOUNT_ID` 对应的账户下。补齐权限/账户后重跑；若暂时不想处理，可把 `workers_dev = true` 并去掉 `[[routes]]`，先用 `*.workers.dev` 地址。
- **附件超过 2 GB**：GitHub Release 单个附件上限为 2 GB，超大的包只能改用其它存储（例如 R2）。
- **`remove_old`**：会删除不再被数据库引用的旧包附件，默认关闭。

## 安全提示

构建过程会在 CI 中执行 AUR 上的 `PKGBUILD`。AUR 包由社区维护，请只构建你信任的包，并留意
`packages.toml` 的变更。`makepkg` 以非 root 的 `builder` 用户运行，且不会继承 `GH_TOKEN`，
因此 `PKGBUILD` 无法直接读取发布用的令牌；但请仍然只构建可信来源的包。
