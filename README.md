# EduAgent

一个对话里的家教。学生用自然语言提问，页面上只有家教在说话。

讲题、记错题、复习和查会过时的事实都从这句话里发生。没有单独的出题入口，也没有第二个说话的人。

## 能做什么

- 打开对话时先打个招呼。今天有要复习的题时，改成一句短简报。
- 年级、姓名、年龄、性别、身高、体重有缺时，招呼和前五轮里各轻轻问一句。问过不再追问。
- 讲完不主动出变式。学生明确要变式、换一道或再来一道类似的题时，才给一个很小的变式。
- 说「记下」「记进错题」时写入错题本。照片里的解题过程做错了、又适合当前年级时，也会记下。
- 用题目里的说法找回错题。每个关键词都要出现在题干或原文里。
- 计算题，或学生要求检查时，这轮话说完再核对。关键步骤有误时，在同一条回复后面改口。
- 考纲、分数线和招生会去查来源。查到了写出来源和链接；没有依据就说不能确定。
- 要这周的安排时，先说排好后再讲。排完由家教再补一句。进程重启后从已经记下的步数继续。

对学生说话的语气是温和、耐心，一步一步带着走。

## 运行

需要 Python 3.11 或更高、Node.js 20、Docker。

数据库：

```bash
docker compose up -d
```

这会在本机 `5432` 端口启动带 pgvector 的 PostgreSQL 16。库名、用户名和密码都是 `eduagent`。`5432` 已被占用时，改 `docker-compose.yml` 里的宿主机端口，并让下面的 `DATABASE_URL` 使用同一个端口。

配置写在仓库根目录的 `.env`，不要提交。Docker Compose 不读这个文件。

```bash
cp .env.example .env
```

Windows 可以用：

```powershell
Copy-Item .env.example .env
```

填上 `LLM_BASE_URL` 和 `LLM_API_KEY`。接口需要兼容 OpenAI 的对话补全。密钥留空时服务可以启动，但不会去调用模型。

接口：

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e backend
cd backend
..\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

macOS 和 Linux 把 `.venv\Scripts\python` 换成 `.venv/bin/python`。

页面：

```bash
cd frontend
npm install
npm run dev -- --hostname 127.0.0.1 --port 3000
```

打开 http://127.0.0.1:3000 。页面默认请求 http://127.0.0.1:8000 。接口不在这个地址时，设置 `NEXT_PUBLIC_API_BASE`。

健康检查是 http://127.0.0.1:8000/health 。它只说明密钥有没有填写，不回传密钥。

## 配置

| 文件 | 放什么 |
| --- | --- |
| 仓库根目录 `.env` | 数据库地址、模型地址、密钥、模型名 |
| `backend/config.toml` | 讲法、身份询问、错题、检索、复核、周计划和招呼 |

这两处都在进程启动时读入。改完需要重启接口。

当前只有一个本地用户，没有登录。

## 许可

[MIT](LICENSE)
