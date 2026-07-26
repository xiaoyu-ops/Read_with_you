import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

function read(relativePath) {
  return fs.readFileSync(path.join(frontendDir, relativePath), "utf8");
}

test("Pet and Agent share the same conversation hook and renderer", () => {
  const petSource = read("components/PetAssistant.tsx");
  assert.match(petSource, /useAgentConversation/);
  assert.match(petSource, /AgentConversationMessages/);
  for (const privatePath of [
    "streamAgentChatMessage",
    "resumeAgentRunStream",
    "cancelAgentRun",
    "getAgentChat",
  ]) {
    assert.equal(
      petSource.includes(privatePath),
      false,
      `PetAssistant should not maintain a private ${privatePath} path`,
    );
  }
});

test("shared conversation core preserves resumable permission and stream state", () => {
  const hookSource = read("components/agent/useAgentConversation.ts");
  assert.match(hookSource, /resumeAgentRunStream/);
  assert.match(hookSource, /waiting_permission/);
  assert.match(hookSource, /pendingUserMessage/);
  assert.match(hookSource, /event\.event === "delta"/);
  assert.match(hookSource, /event\.event === "agent_event"/);
  assert.match(hookSource, /event\.event === "tool_event"/);
  assert.match(hookSource, /event\.event === "done"/);
});

test("Agent message meta has explicit result, permission, trace and context fields", () => {
  const apiSource = read("lib/api.ts");
  assert.match(apiSource, /interface AgentChatMessageMeta/);
  assert.match(apiSource, /result_data\?: AgentRunResultData/);
  assert.match(apiSource, /permission_request\?: AgentPermissionRequestMeta/);
  assert.match(apiSource, /tool_trace\?: AgentToolTraceMeta/);
  assert.match(apiSource, /client_context\?: Record<string, unknown>/);
});

test("paper Agent route uses the research workspace and keeps management separate", () => {
  const paperRoute = read("app/agent/[id]/page.tsx");
  const landingRoute = read("app/agent/page.tsx");
  const manageRoute = read("app/agent/manage/page.tsx");
  const workspace = read("components/agent/AgentWorkspace.tsx");

  assert.match(paperRoute, /AgentWorkspace/);
  assert.match(landingRoute, /AgentLandingPage/);
  assert.match(manageRoute, /AgentManagementPage/);
  assert.match(workspace, /agent-workspace-sessions/);
  assert.match(workspace, /agent-workspace-chat/);
  assert.match(workspace, /agent-workspace-inspector/);
  assert.match(workspace, /useAgentConversation/);
  assert.match(workspace, /aria-label="会话菜单"/);
  assert.match(workspace, /确认清空/);
  assert.doesNotMatch(workspace, /最近后台任务/);
});

test("legacy AgentDrawer is removed after the shared workspace migration", () => {
  assert.equal(fs.existsSync(path.join(frontendDir, "components/AgentDrawer.tsx")), false);
});

test("workspace supports persisted resize and responsive evidence panels", () => {
  const workspace = read("components/agent/AgentWorkspace.tsx");
  const styles = read("app/globals.css");

  assert.match(workspace, /peinidu\.agent\.left-width/);
  assert.match(workspace, /peinidu\.agent\.right-width/);
  assert.match(workspace, /role="separator"/);
  assert.match(workspace, /ArrowLeft/);
  assert.match(workspace, /ArrowRight/);
  assert.match(styles, /@media \(min-width: 1280px\)/);
  assert.match(styles, /@media \(min-width: 768px\) and \(max-width: 1279px\)/);
  assert.match(styles, /@media \(max-width: 767px\)/);
  assert.match(styles, /data-mobile-panel="inspector"/);
});

test("Pet hands the current reader context to the same paper workspace", () => {
  const petSource = read("components/PetAssistant.tsx");
  const workspace = read("components/agent/AgentWorkspace.tsx");
  const handoff = read("lib/agentWorkspaceHandoff.ts");

  assert.match(petSource, /进入研究工作台/);
  assert.doesNotMatch(petSource, />\s*清空\s*</);
  assert.match(petSource, /saveAgentWorkspaceHandoff/);
  assert.match(petSource, /\/agent\/\$\{encodeURIComponent\(paper\.arxiv_id\)\}/);
  assert.match(workspace, /readAgentWorkspaceHandoff/);
  assert.match(workspace, /clearAgentWorkspaceHandoff/);
  assert.match(workspace, /conversation\.sendMessage\(conversation\.input, agentContext\)/);
  assert.match(handoff, /MAX_HANDOFF_BYTES = 24_000/);
});
