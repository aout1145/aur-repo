# AUR 自动构建仓库

基于 GitHub Actions 的 AUR 包自动构建与发布系统：把 AUR 包编译成二进制包，上传到
Cloudflare R2，并生成可以直接添加进 `pacman` 的软件仓库。

- **配置简单**：根目录一个 [`packages.toml`](packages.toml) 决定要构建哪些包。
- **正确处理 AUR 依赖**：自动解析依赖图（包括只存在于 AUR 的依赖），按拓扑顺序先构建
  依赖再构建目标包。
- **按需更新**：只有「仓库里没有」或「AUR 版本比已发布的更新」时才会重建；定时任务也只
  是检查版本，**不会无条件重新构建**。
- **可签名**：支持用 GPG 对软件包和数据库签名，并自动发布公钥。
- **对象存储**：产物上传到 Cloudflare R2（S3 兼容），通过公开域名对外提供服务。

---

## 工作原理

每次运行分成两个 job：

| Job | 作用 |
| --- | --- |
| `plan` | 下载已发布的仓库数据库，解析 `packages.toml` 中所有包（含递归 AUR 依赖）的版本，和已发布版本比较，决定要构建哪些包，输出 `plan.json`。 |
| `build` | 按依赖顺序构建（`makepkg`），把构建出的包安装进容器供后续依赖使用，更新本地仓库数据库，最后上传到 R2。 |

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

### 按需更新逻辑

对每个包，`plan.py` 采用如下判断（`force` 优先）：

- 未在仓库中 → 构建；
- AUR 版本 > 已发布版本（`vercmp`）→ 构建；
- VCS 包（源码 URL 形如 `git+…`）→ 默认不因版本比较而构建，只有开启 `update_vcs` 或 `force` 才构建；
- 可选 `rebuild_dependents = true`：当某个 AUR 依赖本次被重建时，一并重建依赖它的包。

已发布版本来自 R2 上的 `<repo>.db`，因此运行器是「无状态」的，不需要在仓库里 commit 任何东西。

---

## 快速开始

### 1. 准备 Cloudflare R2

1. 新建一个 R2 bucket，例如 `aur-repo`。
2. 打开公开访问：
   - **推荐**：绑定自定义域名（例如 `repo.example.com`），走 Cloudflare CDN；
   - 或临时使用 R2 的公开开发域名 `https://pub-xxxx.r2.dev`（有速率限制，仅测试用）。
3. 创建一个 R2 API Token（权限：**Object Read & Write**；如果启用 `remove_old` 还需 Delete）。
   记下：
   - `Access Key ID`
   - `Secret Access Key`
   - Endpoint，形如 `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`

### 2. 配置 GitHub Secrets / Variables

在仓库 **Settings → Secrets and variables → Actions** 中添加：

Secrets：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `R2_ACCESS_KEY_ID` | 是 | R2 API Token 的 Access Key ID |
| `R2_SECRET_ACCESS_KEY` | 是 | R2 API Token 的 Secret |
| `R2_ENDPOINT` | 是 | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `GPG_KEY` | 否 | 签名用 GPG 密钥 ID（启用签名时必填） |
| `GPG_PRIVATE_KEY` | 否 | ASCII armor 的私钥内容（启用签名时必填） |
| `GPG_PASSPHRASE` | 否 | 私钥口令（若私钥无口令则留空） |

Variables：

| 名称 | 必填 | 说明 |
| --- | --- | --- |
| `R2_BUCKET` | 是 | bucket 名称，例如 `aur-repo` |
| `R2_PREFIX` | 是 | 仓库在 bucket 中的目录，建议设为架构名，例如 `x86_64` |

> `R2_PREFIX` 与下面的客户端 `Server` 地址要对应。建议 `R2_PREFIX = x86_64`，客户端用
> `Server = https://repo.example.com/$arch`。

### 3. 编辑 `packages.toml`

```toml
packages = [
  "paru",
  "aurutils",
  "mpv-full-git",   # 会自动带上 AUR 依赖 ffmpeg-git
]

[repo]
name = "custom"     # 数据库 -> custom.db，客户端配置 [custom]
```

参考文件内注释，可用的全局/单包选项：

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

### 4. 运行

- 进入 **Actions → Build AUR repository → Run workflow**：
  - `packages`：只构建其中的包（空格或逗号分隔，留空表示全部）；
  - `force`：即使已是最新也重建；
  - `update_vcs`：同时重建 `-git` 等 VCS 包。
- 或直接 push 修改到 `packages.toml`。
- 定时任务按需自动更新。

### 5. 客户端使用

给 `pacman.conf` 增加仓库（示例 `R2_PREFIX=x86_64`）：

```ini
[custom]
SigLevel = Required DatabaseOptional
Server = https://repo.example.com/$arch
```

**已签名**（推荐）时，先导入公钥（公钥会随仓库发布为 `<前缀>/repo.gpg`）：

```bash
curl -fsSL https://repo.example.com/x86_64/repo.gpg -o /tmp/repo.gpg
sudo pacman-key --add /tmp/repo.gpg
sudo pacman-key --lsign-key <你的GPG_KEYID>
```

**未签名**时使用：

```ini
[custom]
SigLevel = Optional TrustAll
Server = https://repo.example.com/$arch
```

然后：

```bash
sudo pacman -Syu
sudo pacman -S <包名>
```

---

## 可选：配置 GPG 签名

```bash
# 1. 生成一个无口令（或带口令）的签名密钥
gpg --batch --passphrase '' --quick-generate-key "My AUR Repo <me@example.com>" rsa4096 sign never
gpg --list-secret-keys --keyid-format=long     # 记下 KEYID

# 2. 导出私钥（无口令时直接导出即可）
gpg --armor --export-secret-keys <KEYID>
```

把输出填到 `GPG_PRIVATE_KEY`，`GPG_KEY` 填 KEYID；若私钥有口令，设置 `GPG_PASSPHRASE`。
CI 会用该密钥对每个软件包和数据库做分离签名，并发布 `repo.gpg`。

---

## 目录结构

```
.
├── packages.toml              # 要构建的 AUR 包列表 + 全局/单包配置
├── .github/workflows/build.yml# plan / build / publish 流水线
└── scripts/
    ├── aur_lib.py             # AUR 客户端、依赖解析、仓库数据库读取、版本比较
    ├── plan.py                # 生成构建计划（决定重建哪些包）
    ├── build.py               # 按依赖顺序构建并组装本地仓库
    └── publish.py             # 上传到 R2（可选清理旧版本）
```

---

## 本地测试

脚本只依赖 Python 标准库、`pacman`、`vercmp`、`bsdtar`（`libarchive`）、`git`、`rclone`。

```bash
# 只生成构建计划，不构建
python3 scripts/plan.py --config packages.toml --out plan.json

# 查看计划（会打印每个包是构建还是跳过以及原因）
# 演练构建流程（不真正执行 makepkg / rclone）
python3 scripts/build.py --config packages.toml --plan plan.json --dry-run

# 演练上传（未配置 R2 时会直接跳过）
python3 scripts/publish.py --config packages.toml --dry-run
```

只想构建某个包：

```bash
python3 scripts/plan.py --config packages.toml --packages paru --out plan.json
```

---

## 故障排查

- **`plan` 显示某些依赖无法解析**：该依赖可能来自非官方仓库（如 `archlinuxcn`）。官方仓库没有
  且 AUR 也没有的依赖无法构建，需要手动处理。
- **`makepkg` 在容器里以 root 失败**：workflow 会创建 `builder` 用户并让 `makepkg` 以该用户运行；
  本地运行 `build.py` 时请不要用 root。
- **R2 首次运行没有数据库**：属于正常情况，`plan` 会把所有配置的包标记为「未发布」并构建。
- **客户端报签名错误**：确认已 `pacman-key --lsign-key`，或临时把 `SigLevel` 改宽松。
- **`remove_old` 删除了文件**：请确保 R2 token 具备 Delete 权限；该功能默认关闭。

## 安全提示

构建过程会在 CI 中执行 AUR 上的 `PKGBUILD`。AUR 包由社区维护，请只构建你信任的包，并留意
`packages.toml` 的变更。
