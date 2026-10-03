# 远控时交付到当前轮次面板

客户端的改动面板由原生文件编辑事件驱动。单纯通过 shell 写文件不保证生成该事件；不能用插件或捏造事件强制刷新 UI。

用户在远控或不方便打开本地文件时，按以下方式尝试：

1. 运行 `scripts/export_history.py --prepare-panel`。这一步生成私有临时补丁，返回最终输出路径、`patch_file` 和 `patch_characters`，还没有写入最终 Markdown。
2. 读取补丁，用 Codex 原生 `apply_patch` 工具执行，使最终 Markdown 的新建或更新成为当前轮次的文件改动。不要先用 shell 写最终文件再触碰一行，那样面板可能只显示一行。
3. 验证文件已保存，设置导出文件权限为 `0600`，删除这次生成的临时补丁。若原生工具不可用或执行失败，直接运行普通导出完成保存。
4. 回复中给出文件链接与摘要。只能说明“已通过文件编辑工具发布，可在本轮文件改动里查找”，不能在未观察到客户端 UI 时声称已经显示。若用户仍看不到或面板不可用，直接在回复中发送链接和简短的文件变更列表；用户指定某轮或某文件时，可直接贴对应差异。

同一会话更新原有 Markdown 时，面板可能显示它相对上次导出的差异，完整历史仍保存在该文件中。

脚本会排除导出目录下 `codex-diff-*.md` 的文件变更，防止将上次导出的整份历史再次嵌入下一份历史。不要改成导出完整 JSONL。

## 本环境可用的工具编排示例

`functions.exec` 提供 `tools.exec_command` 和 `tools.apply_patch` 时，可在一次调用中按块读取补丁并发布，避免将大型补丁打印进模型上下文。下面的 `command` 使用默认个人技能目录；安装位置不同时换成实际脚本路径，指定其他会话或目录时追加对应参数。

```javascript
const quote = value => "'" + value.replace(/'/g, "'\\''") + "'";
const command = 'python3 "${CODEX_HOME:-$HOME/.codex}/skills/codex-diff-history/scripts/export_history.py"';
const prepared = await tools.exec_command({cmd: command + " --prepare-panel", max_output_tokens: 2000});
if (prepared.exit_code !== 0) throw new Error(prepared.output);
let plan = JSON.parse(prepared.output);
const patchFile = plan.patch_file;
try {
  if (patchFile) {
    let patch = "";
    for (let offset = 0; offset < plan.patch_characters; offset += 2500) {
      const reader = "import json,pathlib,sys; s=pathlib.Path(sys.argv[1]).read_text(); print(json.dumps(s[int(sys.argv[2]):int(sys.argv[2])+2500],ensure_ascii=False))";
      const chunk = await tools.exec_command({cmd: "python3 -c " + quote(reader) + " " + quote(patchFile) + " " + offset, max_output_tokens: 10000});
      if (chunk.exit_code !== 0) throw new Error(chunk.output);
      patch += JSON.parse(chunk.output);
    }
    const result = await tools.apply_patch(patch);
    if (result?.isError) throw new Error("Native file edit failed");
  }
  const verify = "import hashlib,pathlib,sys; p=pathlib.Path(sys.argv[1]); assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[2]; p.chmod(0o600)";
  const checked = await tools.exec_command({cmd: "python3 -c " + quote(verify) + " " + quote(plan.output) + " " + quote(plan.content_sha256), max_output_tokens: 1000});
  if (checked.exit_code !== 0) throw new Error("Saved content verification failed");
  text({panel_file_edit: "published", output: plan.output});
} catch (error) {
  const fallback = await tools.exec_command({cmd: command, max_output_tokens: 2000});
  if (fallback.exit_code !== 0) throw new Error(fallback.output);
  plan = JSON.parse(fallback.output);
  text({panel_file_edit: "unavailable", saved_by_fallback: true, output: plan.output});
} finally {
  if (patchFile) {
    const cleanup = "import pathlib,sys; pathlib.Path(sys.argv[1]).unlink(missing_ok=True)";
    await tools.exec_command({cmd: "python3 -c " + quote(cleanup) + " " + quote(patchFile), max_output_tokens: 1000});
  }
}
text(plan);
```

工具报错时先检查结果，再走普通导出兜底；不要把失败当作面板发布成功。读取补丁时不展示原始会话或无关工具输出。
