// 触发条件脚本（OpenClaw 无头 JS）：只有 /sessions/pending 里出现"新的"待确认会话时才唤醒模型，
// 没有待处理就不产生任何模型调用，避免每 3 分钟白烧额度。
const r = await exec({ command: "/opt/assistant/skills/session-confirm/scripts/gate.sh pending" });
if (r.status !== "completed" || r.exitCode !== 0) {
  throw new Error("读取 /sessions/pending 失败；到服务器上检查网关");   // 失败要报错，不能当成"没有待办"
}
const body = JSON.parse(String(r.aggregated).trim().split("\n")[0]);
const ids = (body.items || []).map((i) => i.id).sort();
const seen = (trigger.state && trigger.state.ids) || [];
const fresh = ids.filter((id) => !seen.includes(id));
return {
  fire: fresh.length > 0,
  ...(fresh.length > 0 ? { message: "待确认的纳管会话：" + fresh.join("、") } : {}),
  state: { ids },
};
