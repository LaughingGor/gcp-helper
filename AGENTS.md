# 项目协作指南

本文件适用于整个仓库。修改前以实际源码、依赖清单和锁文件为准；功能、配置或命令发生变化时同步更新本文件及相关 README 内容。

## 项目与技术栈

GCP Helper 是前后端分离的 GCP 环境及凭据管理应用，包含多环境 gcloud 登录、初始化任务、Service Account/Gemini 凭据上传、new-api 后台配置、渠道推送与消耗统计，以及用户和角色管理。

- 后端：Python >=3.11，uv 管理依赖（`backend/pyproject.toml`、`backend/uv.lock`，非 Python 可安装包）。FastAPI 0.115.6、Uvicorn 0.34.0、Pydantic 2.10.4、pydantic-settings、SQLAlchemy 2.0.36；pexpect 管理交互登录，cryptography 处理加密。默认 SQLite，依赖也包含 PostgreSQL 驱动 psycopg2。
- 前端：Vue 3.5、TypeScript 5.7（strict）、Vite 6、Element Plus 2.9、Vue Router 4、Pinia 2、Axios。npm 管理依赖，提交 `package-lock.json`。
- 部署：Docker Compose；后端镜像使用 Python 3.11、uv、Google Cloud CLI 和 tini；前端使用 Node 24 构建，Nginx 提供静态文件与反向代理。
- 测试：后端使用 Python 标准库 `unittest` 和 `unittest.mock`，部分 API 测试使用 FastAPI `TestClient`。当前没有配置 pytest、前端测试框架、独立 lint 或格式化命令。

## 代码结构

```text
backend/
  app/
    main.py          FastAPI 入口、路由注册、启动恢复与消耗同步 worker 生命周期
    cli.py           管理命令（如创建管理员）
    api/             HTTP 路由、权限依赖与请求处理
    schemas/         Pydantic 请求及响应结构
    models/          SQLAlchemy 模型
    services/        登录、预设任务、后台配置、凭据、渠道与统计业务逻辑
    runners/         gcloud 普通命令及交互登录进程封装
    core/            配置、鉴权、日志、北京时间日期过滤
    db/              engine、Session、建表与兼容升级
  tests/             test_*.py，unittest 测试
  script/socks5.sh    Compute VM startup script
  run_dev.sh         开发启动脚本
frontend/
  src/
    views/           页面组件
    api/             Axios 客户端与各业务 API
    stores/          Pinia 实例与登录状态
    router/          路由与访问控制
    composables/     分页、目标后台选择等复用逻辑
    utils/           北京时间、剪贴板工具
    App.vue          应用布局
    main.ts          应用入口
    style.css        全局样式
  vite.config.ts     开发服务与代理
  nginx.conf         生产静态站点与代理配置
deploy/              部署持久化数据、备份等运行目录
docker-compose.yml   data-init、backend、frontend 服务
update_channel_models.py  独立 new-api 模型批量更新工具，默认 dry-run
```

`backend/data/` 和 `deploy/data/` 是运行数据，不是源码；检索时排除它们以及 `.venv/`、`node_modules/`、`dist/`。不要遍历凭据目录或读取真实 `.env` 来分析项目，配置说明使用 `.env.example`、`.env.production.example` 和 `core/config.py`。

`.gitignore` 排除本地环境配置、运行数据、数据库、日志、凭据目录、常见私钥文件、依赖及构建产物；保留 `.env.example`、`.env.*.example` 和依赖锁文件。任意命名的凭据 JSON 必须存放在被忽略的数据或凭据目录中，不能依赖文件扩展名自动排除。

## 安装与开发

以下命令除特别说明外均从仓库根目录开始；后端必须在 `backend/` 中运行，配置和默认数据路径相对当前工作目录解析。

首次配置后端（已有 `.env` 时不要覆盖）：

```bash
cd backend
cp .env.example .env
uv sync --frozen
uv run python -m app.cli create-admin --username admin
uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

管理员创建命令交互式输入密码，最低 10 位，没有默认账号密码。后续开发可在 `backend/` 执行 `./run_dev.sh`，其实际调用为 `uv run fastapi dev app/main.py --host 0.0.0.0 --port 8000 --reload-dir app`。真实 GCP 操作还需要可执行的 `gcloud`。

前端另开终端：

```bash
cd frontend
npm ci
npm run dev
```

前端端口为 5173，Vite 将 `/api` 与 `/health` 代理到 `http://127.0.0.1:8000`。API 客户端默认 baseURL 为 `/api`，可用 `VITE_API_BASE` 调整。

后端新增依赖使用 `uv add` / `uv add --dev`，同时更新 `pyproject.toml` 和 `uv.lock`；前端依赖变更同步 `package.json` 和 `package-lock.json`。

## 测试与验证

先安装依赖。后端测试在 `backend/` 执行；导入配置就可能创建目录，因此在启动 Python 前显式隔离数据路径，不使用开发或生产数据库：

```bash
cd backend
test_root=$(mktemp -d /tmp/gcp-helper-tests.XXXXXX)
export DATABASE_URL="sqlite:///$test_root/test.db"
export GCLOUD_BASE_DIR="$test_root/gcloud"
export LOG_FILE="$test_root/app.log"
export NEW_API_CHANNEL_TEMPLATE_DIR="$test_root/templates"
export PYTHONDONTWRITEBYTECODE=1
uv run python -m unittest discover -s tests -p 'test_*.py' -v
```

这些环境变量仅用于测试终端；不要在同一终端直接启动日常开发服务。已有同步好的虚拟环境时，可将 `uv run python` 换为 `.venv/bin/python`，避免 uv 同步环境。

在同一个测试终端运行单个测试文件：

```bash
uv run python -m unittest discover -s tests -p 'test_channel_upload_api.py' -v
```

测试通常使用内存 SQLite、临时目录和 mock；新增测试沿用现有模式，mock gcloud、远端 HTTP、后台任务和等待，避免真实创建云资源或推送渠道。修改鉴权、归属或凭据接口时覆盖权限拒绝、跨用户访问、CSRF 及敏感字段不泄漏等行为。

前端类型检查与构建（从仓库根目录进入）：

```bash
cd frontend
npm run build
```

此命令实际执行 `vue-tsc --noEmit && vite build`，不是单元测试。没有 `npm test` 或 `npm run lint` 脚本。只修改文档时核对路径、命令和源码即可，无须启动服务或访问云端；报告验证时区分实际执行结果与仅核对的命令。

## 修改约定与关键约束

- 保持现有分层：HTTP 逻辑在 `api/`，业务在 `services/`，命令执行在 `runners/`，输入输出在 `schemas/`；前端请求通过 `src/api/http.ts`，复用其 Cookie、CSRF 和 401 处理。
- 遵循相邻文件的 Python 类型标注、SQLAlchemy 2 写法和 Vue Composition API/TypeScript 风格。前后端字段名、枚举和状态流转必须同步核对。
- API 默认前缀 `/api`，健康检查为 `/health`。权限在后端强制校验，不能只靠菜单或路由隐藏。保留 ADMIN、OPERATOR、CHANNEL_USER 的权限与资源归属边界。
- “我的渠道”页面仅管理员和 CHANNEL_USER 可进入，OPERATOR 隐藏菜单且路由跳回环境列表；此页面入口限制不改变既有后台 API 权限与数据归属。
- 登录使用 HttpOnly Session Cookie，写操作校验 CSRF，前端发送 `X-CSRF-Token`。密码使用 scrypt；不得把 Session、授权码、管理 Token、代理密码或凭据明文写入日志或提交仓库。
- `APP_SECRET_KEY` 用于加密等凭据逻辑，统一通过 secret_service 的 get_app_secret_key 读取（进程环境变量优先，其次 backend 工作目录的 .env），Settings 使用 SecretStr 避免明文回显。生产必须稳定并与数据库一起备份；不能随意轮换或把开发默认值当生产配置。直接上传凭据长期加密保存，上传本身不触发远端推送，推送由授权操作显式发起。
- 每个 GCP 环境使用独立 `CLOUDSDK_CONFIG`。命令入口保留参数校验和白名单，不接受任意 shell 命令；测试不得复用真实 gcloud 授权目录。
- 登录进程和部分任务状态保存在后端进程内，保持单 worker、单后端副本；当前没有 Redis/Celery 等持久化任务队列。修改任务逻辑时保留中断恢复、幂等和重试语义。
- 数据库初始化使用 `Base.metadata.create_all` 加 `app/db/init_db.py` 中的兼容升级逻辑。虽然声明了 Alembic 依赖，当前没有 Alembic 迁移目录或配置，不能声称已有 `alembic upgrade head` 工作流。改模型需同时考虑已有数据库升级。旧版本只读账号启动升级时转换为已禁用的 OPERATOR 并撤销会话，保留资源归属；不得自动启用这些账号。
- 日期筛选与展示沿用北京时间（`Asia/Shanghai`）约定；统计金额沿用后台的 quota 换算比例与币种，不等同于供应商账单。
- new-api 后台配置接口接受 baseUrl、token 和可选 statsGroup；名称由地址生成，模型、推送分组、区域沿用默认 Vertex 模板，金额换算沿用系统配置。后台列表显示地址、状态及删除操作，点击地址编辑。保存时启用后台，已有推送的金额与币种快照不得覆盖。删除仅限管理员并校验 CSRF，关联推送、历史渠道或旧版配置时拒绝删除；删除清空用户默认选择和绑定且不操作远端渠道。
- 统计分组 statsGroup 按后台单独保存，与推送分组独立；统计接口使用后台配置，不接受 group 请求覆盖。旧数据库启动补充 stats_group 并从原分组初始化；编辑省略该字段必须保留原值。
- new-api 渠道管理是独立菜单（/new-api-targets），与用户管理同级；凭据管理单独保留。管理员通过用户管理为用户绑定单个后台（bound_target_id），普通用户不能修改绑定或默认后台，推送和统计只能使用绑定后台，管理员可改选。未绑定或后台不可用时后端拒绝；历史部署与消耗不因重新绑定而迁移。旧默认偏好不自动升级成管理员授权绑定。
- new-api 后台新增和编辑保存前须用候选 Token 调用只读渠道列表接口验证；留空编辑验证原 Token。严格校验 success 与列表结构，验证失败保留原配置，不回显 Token 或远端响应正文。
- new-api 后台身份、凭据归属与远端渠道 ID 的关联必须保留；目标后台地址创建后不可修改，重试不能盲目重复创建渠道，历史渠道按现有核对流程关联。
- “新建项目并替换渠道”只更新当前选中后台中已明确关联的唯一渠道，保留远端渠道 ID、部署 ID、累计消耗及币种/比例快照；无关联或存在多条历史渠道时拒绝猜测。普通操作员仅能替换本人环境在当前绑定后台中的渠道，写操作同时校验 task:run、channel:push 和 CSRF。
- 项目替换通过 project_replacements 持久化阶段和加密恢复快照；force_new_project 预设任务固定新 Project ID、VM 名称、计划 Zone 和加密代理参数，重试复用同一任务和资源，禁止普通预设重试入口绕过替换阶段。Key 创建前记录远端 Key 集合；新增远端 Key 而本地 JSON 不可用时须核对，不能盲目再创建。
- 替换使用 new-api 渠道详情和更新接口，保留原配置并仅轮换 JSON/代理，成功后测试启用；失败恢复原配置和启停状态，结果不明标记 UNKNOWN。进行中或 UNKNOWN 的环境禁止普通推送、初始化、改账单、控制台命令和清理；重启后须手动核对/重试。旧凭据和云资源保留，不自动删除项目、VM 或 Key；累计消耗按原渠道整个生命周期统计，不按项目重复计费。

## 部署核对

生产准备与完整发布步骤见 `README.md`。Compose 使用 `backend/.env.production`；默认仅向宿主机暴露前端 8080，Nginx 代理后端。`data-init` 初始化挂载目录并设置 UID 10001 的权限，默认持久化位置为 `deploy/data`，可用 `GCP_HELPER_DATA_DIR` 调整。

开发与测试不应自动执行部署、数据库恢复、管理员创建或 `update_channel_models.py --execute`。这些操作可能改变真实用户、渠道或云端资源；文档核对不需要调用它们。
