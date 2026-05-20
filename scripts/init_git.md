# GitHub 初始化

在项目根目录执行：

```bash
git init
git add .
git commit -m "init DoraYmon qqbot framework"
git branch -M main
git remote add origin <你的仓库地址>
git push -u origin main
```

提交前请检查：

```bash
git status
```

确认不要误提交：

- `.env`
- `config.yaml`
- `*.db`
- `logs/`
- 真实 DeepSeek API Key
- QQBot AppID 和 Secret
- 真实聊天记录
- 私有 skill，例如 `skills/friend_text_compact.md` 或 `skills/private/`
