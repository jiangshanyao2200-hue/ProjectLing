# Security

不要在公开 issue、讨论、截图或日志中提交 API Key、Authorization header、聊天历史、memory 或本机路径中的个人信息。

如果发现凭据泄露：

1. 立即在对应 Provider 撤销并轮换密钥。
2. 检查 Git 历史、Release 资产、fork、缓存与本地 clone。
3. 使用净化构建脚本重新生成公开版本并复跑扫描。

PROJECT凌 可以在用户授权后运行命令和修改绝对路径文件。执行前应核对目标路径、命令边界和当前账号权限。

