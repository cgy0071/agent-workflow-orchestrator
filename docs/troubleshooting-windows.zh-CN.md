# Windows 测试、证据与发布排错

1. **测试在深目录失败，或退出后临时目录删不掉。** 先看第一条异常：路径过长时，在较短的临时路径用同一提交和同一测试重跑，记录两次完整路径；短路径通过并不能证明深路径支持。WinError 32 是文件仍被占用：等对应子进程退出，关闭文件或 SQLite 连接，再清理该次测试目录。SQLite 的事务上下文不会自动关闭连接，可用 contextlib.closing。不要删除运行中的工作区。

2. **PowerShell 能找到 Claude，后台却启动失败。** 用 `Get-Command claude -All` 看实际路径。npm 的 claude.ps1 是包装脚本；AWO 在识别受支持的包装形式并确认相邻包内 bin/claude.exe 存在后直接运行原生程序。识别失败先修正安装或指定正确的 exe，不要改用 shell 拼接提示。若已启动却提前停下，检查 worker/reviewer 的 max_tool_turns（Claude 单次任务内的 --max-turns），不要为此增加 max_real_turns（整项计划的外部调用上限）或重复派工。

3. **Windows 报告 SHA-256 与提交内容不同。** 先对原始报告字节计算 SHA-256，与交付清单逐字比对；另用 git cat-file blob COMMIT:PATH 从固定提交读取字节。用 Python subprocess.check_output 捕获，避免 PowerShell 文本重定向改写输出。仅对已知文本，在内存将 CRLF 替换为 LF 后逐字节比较，分别记录两个原始摘要和比较结果；仍不同即停止验收。不要重写原件，也不要对二进制文件做换行转换。

4. **发布时 GitHub TLS 瞬时中断。** 先用 git ls-remote 核对远端 main、标签及带注释标签的解引用提交，并用 gh release view 查询发行页；连接失败不等于远端没有收到。已有本地标签用 git rev-parse TAG^{commit} 核对已测提交，匹配则复用。远端冲突时停止，不强推或重打标签；缺少引用时重试 git push --atomic origin main refs/tags/TAG。确认标签无误且发行页确实不存在，再执行 gh release create TAG --verify-tag。两仓分别核查，最后按正式标签新克隆并联测。不要禁用 TLS 校验。
