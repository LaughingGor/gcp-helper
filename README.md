# GCP Helper

前后端分离的 GCP 环境登录管理 MVP。

仓库的 `.gitignore` 排除本地 `.env` 配置、运行数据、数据库、日志、凭据目录、常见私钥文件、依赖和构建产物；配置示例（`.env.example`、`.env.*.example`）以及 `backend/uv.lock`、`frontend/package-lock.json` 应提交。任意命名的凭据 JSON 仍须存放在被忽略的数据或凭据目录中。

当前已实现的目标链路：

1. 前端可连续创建多个 GCP 环境，填写 Google 账号和代理配置；环境名由系统自动生成。
2. 后端立即返回独立登录会话，并在后台为每个环境创建独立 `CLOUDSDK_CONFIG` 目录。
3. 后端执行 `gcloud config configurations create <config_name> --no-activate`。
4. 后端写入代理配置。
5. 后端启动 `gcloud auth login <account> --no-launch-browser`。
6. 后端解析授权 URL，前端授权中心批量轮询各会话状态。
7. 用户可分别为多个等待中的环境提交授权码。
8. 后端异步把授权码写入对应的 `gcloud` 进程，完成登录。
9. 后端执行 `gcloud auth list` 校验 active account。
10. 环境状态更新为 `AUTHORIZED`。

## 技术栈

### Backend

- Python 3.11+
- FastAPI
- SQLAlchemy
- SQLite 默认，本地快速启动；生产建议 PostgreSQL
- pexpect 管理 `gcloud auth login` 交互进程
- cryptography 加密代理密码

### Frontend

- Vue 3
- TypeScript
- Vite
- Element Plus
- Vue Router
- Axios

## 环境要求

后端机器需要安装并可执行：

```bash
gcloud --version
```

如果你的 gcloud 版本不支持 `--no-launch-browser`，可以在 `.env` 里改：

```bash
GCLOUD_AUTH_BROWSER_FLAG=--no-browser
```

## Docker 单机部署

项目提供单机 Compose 部署：后端容器内包含 Python、uv 和 Google Cloud CLI；前端
容器使用 Nginx 提供静态页面并将 `/api`、`/health` 转发到后端。SQLite、gcloud
授权目录、密钥和日志保存在独立的宿主机目录 `deploy/data`，不会读取开发或测试使用的
`backend/data`，删除或重建容器也不会丢失生产数据。

当前 gcloud 登录进程保存在后端内存中，因此后端镜像固定使用一个 Uvicorn worker，
不要增加副本数或修改 `--workers 1`。

### 1. 主机要求

- Linux x86_64/arm64，推荐 Ubuntu 22.04 或更新版本
- Docker Engine 24+
- Docker Compose Plugin 2.20+
- 至少 2GB 内存、10GB 可用磁盘
- 能访问 Google、PyPI、npm 和 Google Cloud 软件源

确认安装：

```bash
docker --version
docker compose version
```

### 2. 创建生产配置

```bash
cp backend/.env.production.example backend/.env.production
openssl rand -base64 48
```

把生成结果写入 `backend/.env.production` 的 `APP_SECRET_KEY`。这个密钥用于加密代理
密码和渠道 Token，部署后必须保持不变并单独备份。

本地开发从 `backend/.env` 读取 `APP_SECRET_KEY`，进程环境变量优先（即使为空也会覆盖文件值）。
保存后台和上传凭据都会拒绝空值或示例密钥。修改配置后需重启后端；Compose 修改
`backend/.env.production` 后需重新创建后端容器以加载新的环境变量。
已有加密数据时应使用原密钥或备份中的密钥，不要重新生成密钥覆盖，否则旧凭据无法解密。

默认通过 `http://服务器IP:8080` 访问时使用：

```env
AUTH_COOKIE_SECURE=false
```

通过 HTTPS 域名访问时必须改为：

```env
AUTH_COOKIE_SECURE=true
ALLOWED_CORS_ORIGINS=["https://gcp-helper.example.com"]
```

### 3. 准备全新的持久化目录

Linux 上的后端容器使用 UID `10001`：

```bash
mkdir -p deploy/data deploy/backups
```

默认部署不会复制、导入或挂载任何现有测试数据。需要使用其他生产目录时可以设置：

```bash
GCP_HELPER_DATA_DIR=/srv/gcp-helper/data docker compose up -d
```

### 4. 构建镜像

```bash
docker compose build --pull
docker compose run --rm data-init
```

首次构建需要下载 Python、Node、Nginx、uv、项目依赖和 Google Cloud CLI，耗时会比后续
构建长。`data-init` 会自动创建子目录并设置容器用户 UID `10001` 的权限。验证后端
镜像中的 gcloud：

```bash
docker compose run --rm backend gcloud --version
```

### 5. 创建初始管理员

在第一次启动前执行：

```bash
docker compose run --rm backend \
  uv run python -m app.cli create-admin --username admin
```

命令会要求输入两次至少 10 位的密码。已有数据库已经创建管理员时不要重复执行。

### 6. 启动服务

```bash
docker compose up -d
docker compose ps
```

默认访问地址：

```text
http://服务器IP:8080
```

修改监听端口：

```bash
GCP_HELPER_PORT=9090 docker compose up -d
```

只对外暴露前端端口；后端 `8000` 不映射到宿主机，SQLite 也没有独立网络端口。

### 7. 验证部署

```bash
curl -fsS http://127.0.0.1:8080/health
docker compose exec backend gcloud --version
docker compose logs --tail=100 backend
```

健康接口应返回：

```json
{"status":"ok"}
```

随后登录管理页面，依次验证创建环境、获取授权链接和渠道配置保存。

### 日常管理

```bash
# 查看状态
docker compose ps

# 跟踪日志
docker compose logs -f backend
docker compose logs -f frontend

# 重启服务
docker compose restart

# 停止服务（不会删除 deploy/data）
docker compose down

# 启动
docker compose up -d
```

### 升级发布

升级会中断进程内的 gcloud 登录和预设任务。发布前确认没有处于
`WAITING_CODE`、`CODE_SUBMITTED` 或 `RUNNING` 状态的任务，然后执行：

```bash
docker compose stop
docker compose build --pull
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8080/health
```

应用启动时会自动创建新表并执行当前项目已有的 SQLite 兼容升级逻辑。正式迁移到
PostgreSQL 前应改用 Alembic migration。

### 备份与恢复

SQLite 与 gcloud 文件应一起备份。为获得一致备份，先短暂停止后端：

```bash
docker compose stop backend
tar -czf gcp-helper-backup-$(date +%Y%m%d-%H%M%S).tar.gz \
  deploy/data backend/.env.production
docker compose start backend
```

备份包包含数据库、gcloud 凭据、Service Account Key 和 `APP_SECRET_KEY`，必须加密保存，
不得上传到公开存储。

恢复时停止服务，将 `deploy/data` 和 `.env.production` 恢复到原位置，执行一次
`docker compose run --rm data-init` 后再执行 `docker compose up -d`。

### HTTPS 部署

Compose 默认提供 HTTP `8080`，适合本机或受信内网。公网部署建议在宿主机增加 Caddy、
Nginx 或云负载均衡，将 HTTPS 请求转发到 `127.0.0.1:8080`，并设置：

```env
AUTH_COOKIE_SECURE=true
```

同时建议通过主机防火墙仅允许反向代理访问 8080，公网只开放 80/443。

### 常见问题

- `deploy/data` 无法写入：执行 `docker compose run --rm data-init` 修复权限。
- 登录后立即回到登录页：HTTP 部署应使用 `AUTH_COOKIE_SECURE=false`；HTTPS 必须为
  `true`。
- HTTP/IP 部署下浏览器可能禁用现代 Clipboard API；前端会自动回退到兼容复制方式，
  生产环境仍推荐使用 HTTPS。
- gcloud 启动失败：执行 `docker compose exec backend gcloud --version` 检查安装。
- 重启后授权会话失败：这是当前单进程交互模型的预期行为，需要点击“再次授权”。
- 渠道 Token 无法解密：检查 `APP_SECRET_KEY` 是否和保存配置时一致。

## 后端启动

后端 Python 依赖使用 `uv` 管理。

首次启动：

```bash
cd backend
cp .env.example .env
uv sync
uv run python -m app.cli create-admin --username admin
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

`create-admin` 会交互式要求输入至少 10 位的管理员密码。系统没有默认账号或默认密码；
首次创建的管理员会自动接管升级前尚未归属用户的历史环境。

之后也可以直接：

```bash
cd backend
./run_dev.sh
```

常用依赖管理命令：

```bash
# 添加依赖
uv add package-name

# 添加开发依赖
uv add --dev package-name

# 更新锁文件
uv lock

# 按锁文件同步环境
uv sync --frozen
```

默认数据库：

```text
backend/data/gcp_helper.db
```

默认 gcloud 环境目录：

```text
backend/data/gcloud-envs/{env_id}/gcloud
```

默认运行日志：

```text
backend/data/logs/app.log
```

可在 `.env` 中调整：

```bash
LOG_LEVEL=INFO
LOG_FILE=./data/logs/app.log
MAX_CONCURRENT_LOGINS=5
MAX_CONCURRENT_BOOTSTRAP_TASKS=2
COMPUTE_API_PROPAGATION_WAIT_SECONDS=10
SERVICE_ACCOUNT_KEY_POLICY_WAIT_SECONDS=180
ORG_POLICY_ADMIN_ROLE_WAIT_SECONDS=5
ORG_POLICY_API_PROPAGATION_WAIT_SECONDS=10
AGENT_PLATFORM_IAM_PROPAGATION_WAIT_SECONDS=10
AUTH_SESSION_HOURS=12
AUTH_COOKIE_SECURE=false
```

## 前端启动

```bash
cd frontend
npm install
npm run dev
```

打开：

```text
http://localhost:5173
```

## API

除 `/health` 和 `/api/auth/login` 外，管理 API 均需要登录。浏览器使用 HttpOnly
Session Cookie，写操作还必须携带 CSRF Header。

### 系统登录

```http
POST /api/auth/login
GET  /api/auth/me
POST /api/auth/logout
```

系统内置 `ADMIN`、`OPERATOR`、`CHANNEL_USER` 三种角色。管理员可以通过以下接口创建、
禁用用户或调整角色：

```http
GET   /api/admin/users
POST  /api/admin/users
PATCH /api/admin/users/{userId}
```

“渠道用户”（`CHANNEL_USER`）登录后直接进入“我的渠道”，顶部仅显示此入口。
该角色可以上传 JSON / Gemini Key、查看及同步自己的消耗，不能执行后台推送或访问 GCP 环境、
授权、初始化任务、服务账号密钥下载、环境渠道推送、全局渠道统计、个人配置或用户管理。
管理员可在“用户管理”创建该类型用户，或将现有用户的角色改为“渠道用户”。
该角色仅有 `channel:upload` 和 `channel:usage` 两项权限，后端也执行相同限制。
“我的渠道”页面仅向管理员和渠道用户开放；操作员不显示此菜单，直接访问该页面会跳回环境列表。操作员通过环境列表执行推送、查看推送状态，并通过“渠道统计”查看绑定后台的统计。
管理员在“new-api 渠道管理”配置后台，在“凭据管理”决定直接上传凭据的推送目标，用户无需接触后台管理 Token。旧版本只读账号在启动升级时转换为已禁用的操作员，并撤销已有登录会话，保留账号及资源归属；管理员可重新指定角色并启用。PostgreSQL 启动升级同时更新
`userrole` 枚举，数据库账号需要该类型及用户表的修改权限。

### 创建环境并开始登录

```http
POST /api/gcp-envs/login/start
```

```json
{
  "googleAccount": "user@gmail.com",
  "proxy": {
    "enabled": true,
    "type": "socks5",
    "host": "127.0.0.1",
    "port": 7890,
    "username": "",
    "password": ""
  }
}
```

环境名按“用户名-北京时间日期-当天序号”自动生成，例如
`operator1-2026-09-07-001`、`operator1-2026-09-07-002`。每个用户每天独立计数。

同一用户不能为同一个 Google 账号重复创建环境，账号比较会忽略大小写和首尾空格。
如果账号已经存在，接口返回 HTTP `409` 和已有环境信息；前端会提示查看已有环境。需要
重新登录时应使用该环境的“再次授权”，确实需要重建时应先清理旧环境。

### 查询登录会话

```http
GET /api/gcp-envs/login-sessions/{loginSessionId}
```

查询最近的登录会话或仅查询活跃会话：

```http
GET /api/gcp-envs/login-sessions?limit=100
GET /api/gcp-envs/login-sessions?activeOnly=true
```

创建环境和提交授权码接口均快速返回，gcloud 初始化与授权验证在后台执行。当前登录
进程保存在后端内存中，因此部署时应保持单个后端 worker；服务重启后，未完成会话会被
标记为 `BACKEND_RESTARTED`，需要重新发起授权。

### 提交授权码

```http
POST /api/gcp-envs/login-sessions/{loginSessionId}/code
```

```json
{
  "code": "4/xxxx"
}
```

失败、已过期或已取消的最新会话可以复用原环境与代理配置再次授权：

```http
POST /api/gcp-envs/login-sessions/{loginSessionId}/retry
```

同一个环境同时只允许存在一个活跃授权会话。

### 环境列表

```http
GET /api/gcp-envs?createdDate=2026-09-07
GET /api/gcp-envs?startDate=2026-09-01&endDate=2026-09-07
```

日期参数按北京时间（`Asia/Shanghai`）解释，区间包含开始日和结束日；不传日期参数时
返回全部日期。`createdDate` 作为单日查询兼容参数继续保留。

### 清理环境

```http
DELETE /api/gcp-envs/{envId}
```

### 预设任务

```http
POST /api/gcp-envs/{envId}/preset-tasks/bootstrap
```

环境授权成功后，系统会查询当前账号可访问的 Open Billing Account：恰好只有一个时自动选择并
启动预设任务；存在多个或没有可用账户时不自动启动，环境列表会提示“待选择账单”，需要在环境
详情中人工确认后选择。系统不会根据赠金额度自动猜测 Billing Account，管理员需要先在 Google
Cloud Billing Console 确认账户。
预设任务会查询当前账号可访问的 ACTIVE project，并优先
使用最近创建的、Project ID 以 `gcph-` 开头的 project；如果远端不存在这类 project，才会
创建一个新的、归属为 `No organization` 的 project。随后任务会使用选定的 project 启用
Compute Engine、Agent Platform、IAM API，预生成 socks5 端口、用户名和密码，创建一台
Debian Compute VM，把 `backend/script/socks5.sh` 设置为 startup script 并通过 VM metadata
传入 socks5 参数，按 `socks5-server` tag 放行代理端口，并为 Compute Engine 默认 service
account 生成 JSON key。

任务开始时会检查该环境当前 gcloud 配置和数据库记录中的 Project ID。若存在不以
`gcph-` 开头的 Project，会先清除本地 Project 配置及关联状态，再尝试使用远端已有的
`gcph-` Project；远端不存在时创建新的 `No organization` project 并继续执行。

如果创建 Service Account JSON Key 时命中 Key 创建组织策略限制，任务会同时为当前
Project 处理 `constraints/iam.disableServiceAccountKeyCreation` 和
`constraints/iam.managed.disableServiceAccountKeyCreation` 两个约束，创建“不强制执行”
的项目级策略例外。legacy 约束使用 Org Policy V1 命令，managed 约束通过
`gcloud org-policies set-policy` 和 Org Policy V2 JSON 设置。系统会优先直接使用当前
gcloud 登录用户已有的权限修改策略，不再
重复授予角色。只有明确返回权限不足时，才会解析 Project 所属 Organization，并尝试在
Organization 层给该环境的 Google 授权账号授予 `roles/orgpolicy.policyAdmin`，等待
`ORG_POLICY_ADMIN_ROLE_WAIT_SECONDS` 后再次修改策略。自动授予角色需要执行账号具备
`resourcemanager.organizations.setIamPolicy`，通常来自
`roles/resourcemanager.organizationAdmin`。如果没有该组织级权限，只能由组织管理员
手工授予。策略修改成功后等待 `SERVICE_ACCOUNT_KEY_POLICY_WAIT_SECONDS` 再重试创建
Key；约束例外仍然只设置在当前 Project。

如果当前环境已有 Project，重新选择 Billing Account 会立即执行 `billing projects link`，将该
Project 重新关联至所选账户；预设任务运行中不能更换账户。系统仅显示当前授权账号可访问且
处于 OPEN 状态的账户，不会显示或保存赠金余额。

默认 VM 配置（Admin/Operator 可在“默认偏好”设置预填 Zone；任务提交后固定）：

```text
zone: us-west2-a
machine type: e2-micro
image: debian-12 / debian-cloud
boot disk: 10GB pd-standard
network: gcp-helper-network（自动子网模式）
network tag: socks5-server
```

任务不会依赖 GCP 的 `default` 网络。它会显式创建或复用 `gcp-helper-network`，等待 VM
所在 Region 的自动子网可用，并把解析到的子网完整资源地址通过 `--subnet` 传给 VM 创建
命令；防火墙规则也绑定该网络。这可以兼容禁用默认网络创建、或新 Project 中没有
default subnet 的情况。磁盘小于 200GB 的 gcloud 信息是性能警告，不会导致任务失败。

### 新建项目并替换当前后台渠道

环境列表和详情页提供“新建项目并替换渠道”。先选择当前目标后台，弹窗自动展示原渠道 ID、
Project 和环境已选的 Billing Account；仅 VM 区域允许按需调整，无需填写项目 ID、JSON 或代理参数。
普通操作员只能操作本人环境在管理员绑定后台中的渠道，管理员可选择后台。
当前后台没有渠道时须先推送；历史渠道须先明确关联后台。若同一环境在当前后台有多条已关联
历史渠道，系统拒绝猜测替换对象，须先由管理员核对记录。

本操作显式授权新建项目和更新所展示的原渠道：始终创建全新的 `gcph-` Project，使用环境已选
Billing Account，复用 API、网络、SOCKS5 VM、防火墙和 JSON 生成流程。新凭据加密归档后，
通过 `GET /api/channel/{id}` 读取原渠道配置，通过 `PUT /api/channel/` 更新原渠道的 JSON 和代理，
并通过独立启停接口启用、测试渠道。后台须支持这些 new-api 标准接口及已有的渠道测试、启停接口。
原渠道名称、模型、分组、优先级和其他受支持配置保留，其他后台的渠道不受影响。
接口语义已按 [new-api 上游实现](https://github.com/QuantumNous/new-api/blob/main/controller/channel.go) 核对：配置更新不传 `status`，启停单独调用。

替换成功保留原渠道 ID、部署记录、累计消耗、金额换算比例和币种；新旧凭据及替换历史均保留。
累计消耗仍属于该远端渠道的整个生命周期，不按新旧项目拆分，也不新增一份累计消耗。
旧项目、VM 和 Key 不会自动删除，可能继续产生云端费用。切换期间可能短暂中断。

新资源准备失败不修改原渠道；更新或测试失败时尝试用加密快照恢复原 JSON、代理和启停状态。
原先自动禁用的渠道恢复为手动禁用，保持停用状态；多 Key 渠道不支持此替换流程。
若更新或恢复结果不明确，标记“结果待核对”，禁止普通推送、初始化、修改账单、执行控制台命令或
清理该环境。点击“核对 / 恢复”会先恢复原配置；恢复确认后可再次重试替换。
服务重启后保留任务和快照，须手动重试，不自动重复更新远端。
准备新项目时 gcloud 当前配置会指向新项目；环境列表的 Project 在渠道替换成功后更新。
Project ID、VM 名称与代理参数在创建前持久化，重试复用同一批资源；普通预设任务的重试入口不会
执行项目替换任务。若远端已新增 Key 而本地 JSON 丢失，须先核对并清理无用 Key，系统拒绝盲目重复创建。

接口（写操作均要求 Session Cookie、CSRF、`task:run` 和 `channel:push` 权限）：

- `GET /api/gcp-envs/{envId}/project-replacements/preview?targetId={targetId}`：读取当前后台唯一可替换渠道。
- `POST /api/gcp-envs/{envId}/project-replacements`：传 `targetId`、预览返回的 `deploymentId`、UUID `requestId` 和可选 `vmZone`，返回 202。同一操作者的相同请求 ID 复用已有操作。
- `GET /api/gcp-envs/{envId}/project-replacements`：最近 20 条替换记录。
- `GET /api/gcp-envs/{envId}/project-replacements/{id}`：查看步骤和结果，不返回凭据或恢复快照。
- `POST /api/gcp-envs/{envId}/project-replacements/{id}/retry`：失败重试，或先恢复待核对的原配置。

### 查询预设任务

```http
GET /api/gcp-envs/{envId}/preset-tasks/latest
GET /api/gcp-envs/{envId}/preset-tasks/{taskId}
```

后台任务列表也支持北京时间日期过滤：

```http
GET /api/preset-tasks?createdDate=2026-09-07
GET /api/preset-tasks?startDate=2026-09-01&endDate=2026-09-07
```

### 下载 service account JSON key

```http
GET /api/gcp-envs/{envId}/preset-tasks/{taskId}/service-account-key
```

### 统一后台配置与默认偏好

后台连接是平台资源，由管理员在独立的“new-api 渠道管理”菜单统一维护，与“用户管理”“后台任务”同级；“凭据管理”只负责凭据和推送。普通用户不维护个人地址和 Token。
管理员可在“默认偏好”保存默认 VM Zone 与默认后台 ID；普通用户仅可修改 VM Zone。修改偏好或绑定不会改变已提交任务、历史渠道或消耗。
`GET /api/user-settings` / `PUT /api/user-settings` 使用 `computeVmZone`、可空 `defaultTargetId`。
`GET /api/new-api-targets` 向有 `channel:push` 权限的员工返回允许使用的已启用后台选项：管理员返回全部，普通操作员仅返回绑定后台，不额外返回地址字段或 Token。
只有 `ADMIN` 可选择所有已启用后台；`OPERATOR` 只能使用管理员绑定的一个后台。未绑定或绑定后台不可用时提示联系管理员，后端拒绝推送或渠道统计；请求其他后台 ID 返回 403。`CHANNEL_USER` 保留“我的渠道”权限，不因此获得推送或全局统计权限。
管理员在“用户管理 → 绑定后台”设置或解除绑定，接口为 `PUT /api/admin/users/{userId}/target`，请求 `{ "boundTargetId": "后台 UUID" }`，解除绑定传 `{ "boundTargetId": null }`。该写操作要求管理员和 CSRF，返回用户的 `boundTargetId`，并记录审计日志。
升级只增加 `users.bound_target_id`，不把用户过去自行选择的默认后台自动视为授权绑定；升级后需管理员显式绑定。个人偏好接口不能修改绑定，也不能由普通用户修改或清空默认后台 ID。

GCP 授权与直接上传共用加密凭据和推送记录：

- GCP 初始化成功后自动归档生成的 JSON，来源为 `GCP`；直接上传来源为 `UPLOAD`。
- GCP 生成的 SOCKS5 URL 加密保存，并在创建推送记录时固定代理快照，实际发送时才解密。
- 环境列表单条和批量推送：管理员可选择后台，普通用户只显示绑定后台名称。`POST /api/gcp-envs/{envId}/channel-push` 可传 `{ "targetId": "后台 UUID" }`；普通用户省略时使用绑定后台，管理员省略时使用默认偏好或绑定。返回 202 与 `deploymentId`，模板沿用服务端默认模板配置。
- 管理员代操作使用所选后台，凭据及消耗归属环境所有者，操作人单独记录。不会根据操作人的个人 Token 改变目的地。
- 已归档的凭据在原 key 文件丢失后仍可推送。若首次归档失败，推送时会尝试补录。
- 管理员可点击“补录历史 GCP 凭据”，分批扫描成功任务并加密归档本地 key，不执行远程推送。

VM Zone 的解析优先级为：本次任务明确选择 → 环境默认 → 环境所有者默认 → 系统默认。
创建环境的可选 `vmZone` 保存为环境默认；手动启动初始化任务也可传 `{ "vmZone": "us-central1-b" }`。
创建任务时写入 `requestedVmZone`，执行与失败重试均使用该快照；`vmZone` 继续记录实际 VM 所在区域。
用户默认 Zone 不影响已提交任务，VM Zone 与 Vertex 服务区域分别配置。
服务重启会把中断的初始化任务标记为失败，重试保留计划 Zone。重复提交复用活动任务并防止重复执行。

升级启动时自动增加必要字段，旧用户连接复制到后台管理并标记为停用，**管理员核对后编辑并保存地址和 Token 以启用**。
旧配置不删除，也不会覆盖后台管理中已有的 Token。`PUT /api/channel-config` 返回 410，旧只读接口保留。
历史 GCP 推送未记录后台身份，不能按渠道 ID 自动推断所在后台。检测到所选后台的历史同名渠道时，
推送会进入 `LEGACY_REVIEW`，管理员通过“关联历史渠道”明确选择历史记录及后台；核对名称和已知 ID 后
关联到统一推送记录；原记录未保存 ID 时，由管理员明确选择后台后读取并核对现有渠道。不创建渠道，不重复统计。其他凭据已经关联的远程渠道不能重复认领。

### 管理员批量添加渠道模型

仅 `ADMIN` 可以在“渠道模型管理”页面选择统一后台，批量向该后台添加一个模型。
页面会先预览指定分组、状态下缺少该模型的渠道；确认执行时后端会重新读取远端渠道，只更新
`models` 字段并保留 new-api PUT 所需的其他可编辑字段。已有模型的渠道不会重复写入，每次执行
都会写入审计日志。

```http
POST /api/channel-models/preview
POST /api/channel-models/add
```

```json
{
  "targetId": "后台 UUID",
  "group": "Gemini_Vertex_Pro",
  "status": "enabled",
  "model": "gemini-3.1-pro-preview"
}
```

`status` 支持 `enabled`、`disabled` 和 `all`。这两个接口均要求登录、管理员权限和 CSRF Header；
后台需由管理员配置并启用；用户默认后台只作为默认选择。预览与执行请求固定目标后台。

### new-api 渠道统计

`ADMIN` 和 `OPERATOR` 可以通过顶部“渠道统计”页面查看后台数据，使用其“统计分组”配置的实时统计。普通用户自动使用绑定后台，只有管理员显示后台选择下拉框。后端会持续分页读取全部渠道，直到达到接口返回的
`total`、读到空页或检测到服务端重复返回同一页。分页参数按 new-api 的约定从 `p=1`
开始递增，不会因单页数量小于请求的 page size
而提前停止。状态 `2`、`3` 分别作为手动
禁用和自动禁用，状态 `1` 作为启用渠道。

```http
GET /api/channel-statistics
GET /api/channel-statistics?targetId=后台UUID&refresh=true
```

消费金额按 `used_quota / 所选后台的 quotaPerUnit` 换算，使用该后台的币种。页面同时汇总启用渠道已使用总量
（包含正常、额度用尽和超量渠道），以及“启用渠道 + 禁用渠道”的全部有效消费总量。
未知状态和金额异常渠道不参与总量。默认单渠道额度为所选币种的 300 单位（可用 `NEW_API_CHANNEL_BUDGET_AMOUNT` 修改）：启用
渠道消费小于 300 时计算剩余金额，等于 300 时标记额度用尽，大于 300 时标记超量且不
计入正常余量。相关部署参数：

```env
NEW_API_CHANNEL_STATS_GROUP=Gemini_Vertex_Pro
NEW_API_CHANNEL_BUDGET_AMOUNT=300
NEW_API_QUOTA_PER_UNIT=500000
NEW_API_CURRENCY=USD
NEW_API_CHANNEL_STATS_CACHE_SECONDS=30
NEW_API_CHANNEL_STATS_PAGE_SIZE=100
NEW_API_CHANNEL_STATS_MAX_PAGES=100
```

统计缓存按用户、后台、分组、换算比例、币种和 Token 隔离；页面“刷新实时数据”会绕过缓存。换算比例和币种沿用系统 `NEW_API_QUOTA_PER_UNIT`、`NEW_API_CURRENCY` 默认配置。

## 上传凭据持久化、管理员推送与个人消耗

“我的渠道”支持上传 Service Account JSON 文件、粘贴 JSON 或 Gemini API Key。
JSON 最大 64 KB，必须包含有效 RSA 私钥、项目 ID、私钥 ID 和服务账号邮箱。
JSON 完整字段会保留（统一格式并补充默认 Google Token 地址），不接受任意 Token endpoint。
API Key 为单个 Gemini Key，不接收多行批量 Key。

用户上传后先长期加密保存到本项目数据库，状态为“已保存”，**不会自动请求 new-api**。
上传不需要配置后台，也不需要 GCP 环境、gcloud 登录、VM 或 SOCKS5。
`APP_SECRET_KEY` 必须配置为稳定的随机密钥并与数据库一起备份；它用于凭据、后台 Token
加密及去重指纹。新版本不再按 24 小时清理凭据，`CHANNEL_UPLOAD_CREDENTIAL_HOURS`
旧配置已失效。成功推送、测试失败、后台重启均不会删除已保存的凭据。

管理员使用独立菜单“new-api 渠道管理”“凭据管理”和“用户管理”：

1. “凭据管理”展示上传用户、凭据身份、保存状态及各后台的推送记录。
2. 点击“查看”可读取原始 Key 或完整 JSON，点击“下载”可保存 JSON / 文本文件。
3. 在“new-api 渠道管理”填写后台地址、Access Token 和统计分组；后台名称自动使用地址，其余参数沿用默认模板及系统配置。列表显示地址、状态和删除操作，点击地址打开编辑表单。
4. 选择一份凭据，点击“选择后台推送”，默认选择凭据所属用户绑定的后台，管理员可改选或选择多个后台。普通用户不能在凭据管理中执行推送或重试。
5. 展开推送记录查看各后台的渠道 ID、状态、消耗及同步异常；管理员可重试失败或核对超时任务。

每个后台地址创建后不可修改；地址变更需要新增后台，避免把旧渠道 ID 关联到新服务器。
编辑时管理 Token 留空保持原值；Token 在数据库中加密保存，不通过列表接口回显。
新增和编辑保存前，后端使用候选 Token 请求 `GET /api/channel/?p=1&page_size=1`，携带
`Authorization: Bearer <Token>` 验证渠道查看权限。编辑时留空则验证已保存的 Token。
只有返回 `success: true` 且包含有效渠道列表（允许空列表）才保存；验证失败返回 400，
界面显示原因并保留原配置。401/403、网络错误、超时、非 JSON 或异常响应均不会保存，错误提示不回显 Token 或远端响应正文。
该检查只确认当次渠道读取权限，不保证后续创建或修改渠道的权限，也不保证 Token 永不过期。
管理员可点击列表中的“删除”并二次确认，删除未关联推送记录、历史渠道记录或旧版个人后台配置的后台。
删除会清除所有用户对此后台的默认选择及绑定并记录审计日志，不调用远端删除接口。
已有关联数据时返回 409 并保留原数据；后台不存在时返回 404。
新增或编辑保存后台时自动启用；历史停用后台在重新保存前仍停止推送和同步。
Vertex 使用 `NEW_API_UPLOAD_VERTEX_TEMPLATE`，未指定时沿用 `NEW_API_CHANNEL_DEFAULT_TEMPLATE`（默认 `default`）。
模板位于 `NEW_API_CHANNEL_TEMPLATE_DIR`；默认 Vertex 模板缺失或格式错误时拒绝保存后台。
模型、分组、区域从默认 Vertex 模板读取，缺省时分别使用 `NEW_API_UPLOAD_MODELS`、`NEW_API_CHANNEL_STATS_GROUP`、`NEW_API_UPLOAD_VERTEX_REGION`。
Gemini 使用 `NEW_API_UPLOAD_GEMINI_TEMPLATE`；未指定时沿用内置类型 `24`，模型、分组使用上述默认值。
创建或编辑接口接受 `baseUrl`、可选 `token`（新增时必填）及可选 `statsGroup`。
`statsGroup` 为单个统计分组名称，自动去除首尾空格，长度 1～128；省略时，编辑保留原值，新增沿用模板分组。
“渠道统计”只使用所选后台保存的统计分组，接口不再支持通过 `group` 参数覆盖。
统计分组独立于渠道推送分组，不修改模板、远端渠道或历史消耗；修改后刷新统计即可生效。
启动时自动增加 `new_api_targets.stats_group` 并用原分组初始化已有后台，保留升级前的统计范围。
模板采用 `{ "mode": "single", "channel": { ... } }`，服务端覆盖 `name`、`key`、`status`、`mode`，
不执行字符串占位符替换。上线前需要按实际 new-api 版本验证区域、模型及额外参数。

同一份凭据只保存一份密文，各后台推送记录引用该凭据，分别保存渠道 ID、状态和消耗。
同一后台重复推送会复用已有记录；创建超时会先按系统唯一名称查找，无法确认时不重复创建。
测试失败会尝试禁用远程渠道，禁用失败会明确提示管理员核对。测试可能产生少量记账消耗。
查看、下载凭据、修改后台及直接上传凭据的推送均要求管理员权限及 CSRF；GCP 环境推送要求 `channel:push` 和环境访问权限。操作写入不含原始凭据的审计日志。
凭据明文只由专用 POST 接口返回，响应标记 `Cache-Control: no-store`，前端关闭对话框后清空内容。
普通用户的列表和消耗接口不返回原始凭据或后台管理 Token。

升级会增加 `new_api_targets` 表，并为现有 `channel_uploads` 表添加来源上传 ID 和后台 ID，
保留旧渠道、归属和消耗。历史上已清除的凭据无法从本项目恢复，列表显示“历史内容已清除”。
原用户重新上传相同凭据可以补存到原记录，保留渠道历史；不能跨用户认领。
若管理员新增的后台地址与旧渠道地址一致，推送操作会直接关联原渠道记录，避免重复创建和计费统计。
旧环境变量 `NEW_API_UPLOAD_BASE_URL/TOKEN` 等仅继续支持历史记录的后台连接；新上传由
管理页面的后台配置控制。GCP 环境推送、模型管理和“渠道统计”也共用统一后台，不自动认领其他渠道。

个人统计按凭据原上传用户汇总全部推送的渠道，以各后台登记的渠道 ID 为准，不按名称或分组推断。
每份上传可展开查看不同后台的消耗；相同渠道 ID 在不同后台互不冲突。
金额口径为 `new-api used_quota / quotaPerUnit`，不是 Google 或供应商账单。
换算比例与币种在首次推送时固定，后台重新保存时更新为系统默认值，已存在推送记录保留原快照；不同币种分开汇总。
禁用渠道仍计入消耗。远程删除、同步失败、计数器下降时保留最后有效值并显示异常。
默认每 120 秒按后台和管理 Token 合并同步；采样记录默认保留 30 天，累计值长期保留。

```env
CHANNEL_UPLOAD_SYNC_SECONDS=120
CHANNEL_UPLOAD_SNAPSHOT_DAYS=30
```

主要接口：

- `POST /api/channel-uploads/preview`：校验及脱敏预览。
- `POST /api/channel-uploads`：传 `kind`、`credential`、可选 `remark`、UUID `requestId`，返回 201，保存凭据而不推送。
- `GET /api/my-channels`、`GET /api/my-channel-usage`、`POST /api/my-channel-usage/refresh`：本人凭据、推送明细和消耗。
- `GET /api/admin/channel-uploads`：管理员分页查看所有用户的上传。
- `POST /api/admin/channel-uploads/{id}/credential`、`POST /api/admin/channel-uploads/{id}/download`：查看、下载凭据。
- `GET/POST /api/admin/new-api-targets`、`PUT /api/admin/new-api-targets/{id}`：后台配置。
- `DELETE /api/admin/new-api-targets/{id}`：仅管理员可删除未被使用的后台，要求 CSRF；成功返回 204。
- `GET /api/new-api-targets`：管理员可选的全部后台，普通操作员仅绑定后台；不额外返回地址字段或 Token。
- `POST /api/admin/gcp-credentials/import?cursor=...`：每批最多处理 50 个成功任务，返回 `nextCursor`，仅补录本地凭据。
- `GET /api/admin/channel-uploads/{id}/legacy-pushes`、`POST /api/admin/channel-uploads/{id}/adopt-legacy`：核对历史推送及后台身份。
- `POST /api/admin/channel-uploads/{id}/push`：传 `targetIds` 数组（最多 20 个），返回 202，创建或复用各目标推送任务。
- `POST /api/channel-uploads/{id}/retry`：仅管理员可重试或核对已有推送，不接收用户指定的后台地址。
- `GET /api/admin/channel-usage`、`GET /api/admin/channel-usage/{userId}`：管理员按用户汇总、明细。

继续使用单 worker、单后端副本。第一期不提供指定日期的精确结算或供应商账单核算。

## 安全注意

- 用户密码使用带随机盐的 scrypt 哈希保存，原始密码不入库。
- Session Token 仅通过 HttpOnly Cookie 传输，数据库只保存 Token 哈希。
- 写操作使用 SameSite Cookie 和 CSRF Token 双重校验。
- 生产环境必须启用 HTTPS，并设置 `AUTH_COOKIE_SECURE=true`。
- `ADMIN` 可访问全部环境；`OPERATOR` 只能访问归属于自己的环境。
- 授权码不入库，不写日志。
- 代理密码加密保存；生产建议换 Google Secret Manager / Vault。
- 前端不能传任意 gcloud 命令，后端只执行白名单动作。
- 每个环境使用独立 `CLOUDSDK_CONFIG` 目录，目录权限 `0700`。
- 后续 service account key JSON 不要明文落库，必须加密或交给 Secret Manager。

## 后续改进

- 任务日志 SSE。
- 独立持久化任务队列和多副本支持。
- 按日期消费明细与供应商账单核对。
