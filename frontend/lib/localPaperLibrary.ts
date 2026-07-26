"use client";

import { API_BASE } from "./api";

const DB_NAME = "peinidu-local-library";
const DB_VERSION = 1;
const HANDLE_STORE = "handles";
const ROOT_HANDLE_KEY = "paper-library-root";
const MODE_KEY = "peinidu.libraryMode";
const WORKSPACE_MANIFEST = "peinidu-workspace.json";
const MAX_FILES = 2048;
const MAX_FILE_BYTES = 220 * 1024 * 1024;
const MAX_TOTAL_BYTES = 512 * 1024 * 1024;
const MAX_WORKSPACE_PAPERS = 2000;
const MAX_WORKSPACE_COLLECTIONS = 500;

export type LibraryMode = "server" | "local_folder";
export type PortableConflictPolicy = "reject" | "keep_local";

export type PortableFileEntry = {
  path: string;
  size: number;
  sha256: string;
};

export type PortableManifest = {
  version: 1;
  paper_id: string;
  paper: {
    title: string;
    authors: string[];
    source: string;
    status: string;
  };
  revision: string;
  base_revision: string | null;
  bundle_type: "full" | "delta";
  files: PortableFileEntry[];
  included_paths: string[];
  deleted_paths: string[];
  total_bytes: number;
};

export type LocalLibraryState = {
  supported: boolean;
  mode: LibraryMode;
  directoryName: string | null;
  permission: PermissionState | "unavailable";
};

export type LocalWorkspaceCollection = {
  name: string;
  paper_ids: string[];
};

export type LocalWorkspaceManifest = {
  version: 1;
  saved_at: string;
  papers: { paper_id: string; revision: string }[];
  collections: LocalWorkspaceCollection[];
  workspace_revision: string;
};

export type BulkPaperMigrationItem = {
  paper_id: string;
  status: "done" | "error";
  message?: string;
};

export type BulkPaperMigrationResult = {
  items: BulkPaperMigrationItem[];
  completed: number;
  failed: number;
  workspace: LocalWorkspaceManifest | null;
};

type DirectoryPickerOptions = {
  id?: string;
  mode?: "read" | "readwrite";
  startIn?: string;
};

declare global {
  interface Window {
    showDirectoryPicker?: (
      options?: DirectoryPickerOptions,
    ) => Promise<FileSystemDirectoryHandle>;
  }

  interface FileSystemDirectoryHandle {
    queryPermission(options?: { mode?: "read" | "readwrite" }): Promise<PermissionState>;
    requestPermission(options?: { mode?: "read" | "readwrite" }): Promise<PermissionState>;
    entries(): AsyncIterableIterator<[string, FileSystemHandle]>;
  }
}

export class PortableRevisionConflictError extends Error {
  currentRevision: string | null;
  baseRevision: string | null;

  constructor(message: string, currentRevision: string | null, baseRevision: string | null) {
    super(message);
    this.name = "PortableRevisionConflictError";
    this.currentRevision = currentRevision;
    this.baseRevision = baseRevision;
  }
}

export class LocalLibraryPermissionRequiredError extends Error {
  constructor() {
    super("本地文献库需要重新授权，请先在设置中重新选择目录。");
    this.name = "LocalLibraryPermissionRequiredError";
  }
}

export function supportsLocalPaperLibrary(): boolean {
  return (
    typeof window !== "undefined" &&
    window.isSecureContext &&
    typeof window.showDirectoryPicker === "function" &&
    typeof window.indexedDB !== "undefined"
  );
}

export function getLibraryMode(): LibraryMode {
  if (typeof window === "undefined") return "server";
  return window.localStorage.getItem(MODE_KEY) === "local_folder"
    ? "local_folder"
    : "server";
}

export function useServerLibrary(): void {
  if (typeof window === "undefined") return;
  window.localStorage.setItem(MODE_KEY, "server");
}

export async function chooseLocalLibraryDirectory(): Promise<LocalLibraryState> {
  if (!supportsLocalPaperLibrary() || !window.showDirectoryPicker) {
    throw new Error("当前浏览器不支持本地文献库。");
  }
  const handle = await window.showDirectoryPicker({
    id: "peinidu-paper-library",
    mode: "readwrite",
  });
  const permission = await handle.requestPermission({ mode: "readwrite" });
  if (permission !== "granted") {
    throw new Error("没有获得本地文献目录的读写权限。");
  }
  await saveRootHandle(handle);
  window.localStorage.setItem(MODE_KEY, "local_folder");
  return {
    supported: true,
    mode: "local_folder",
    directoryName: handle.name,
    permission,
  };
}

export async function getLocalLibraryState(
  requestPermission = false,
): Promise<LocalLibraryState> {
  const supported = supportsLocalPaperLibrary();
  const mode = getLibraryMode();
  if (!supported || mode === "server") {
    return {
      supported,
      mode: "server",
      directoryName: null,
      permission: supported ? "prompt" : "unavailable",
    };
  }
  const handle = await loadRootHandle();
  if (!handle) {
    useServerLibrary();
    return {
      supported,
      mode: "server",
      directoryName: null,
      permission: "prompt",
    };
  }
  let permission = await handle.queryPermission({ mode: "readwrite" });
  if (permission === "prompt" && requestPermission) {
    permission = await handle.requestPermission({ mode: "readwrite" });
  }
  if (permission === "denied") {
    useServerLibrary();
    return {
      supported,
      mode: "server",
      directoryName: handle.name,
      permission,
    };
  }
  return {
    supported,
    mode,
    directoryName: handle.name,
    permission,
  };
}

export async function syncPaperToLocal(
  paperId: string,
  options: { forceFull?: boolean } = {},
): Promise<PortableManifest> {
  const root = await requireWritableRoot();
  const paperDirectory = await root.getDirectoryHandle(encodeURIComponent(paperId), {
    create: true,
  });
  const currentManifest = await readLocalManifest(paperDirectory);
  const baseRevision =
    options.forceFull || !currentManifest ? null : currentManifest.revision;
  const query = baseRevision
    ? `?base_revision=${encodeURIComponent(baseRevision)}`
    : "";
  const response = await fetch(
    `${API_BASE}/papers/${encodeURIComponent(paperId)}/portable-bundle${query}`,
    { cache: "no-store" },
  );
  if (!response.ok) {
    throw new Error(await responseMessage(response, "保存本地副本失败"));
  }
  const formData = await response.formData();
  const manifest = parseManifestPart(formData.get("manifest"));
  const payloads = formData.getAll("file");
  validateManifest(manifest);
  if (manifest.paper_id !== paperId) {
    throw new Error("服务端返回了错误论文的可移植包。");
  }
  if (
    manifest.bundle_type === "delta" &&
    (!currentManifest || manifest.base_revision !== currentManifest.revision)
  ) {
    throw new PortableRevisionConflictError(
      "本地副本的版本已变化，请重新同步完整服务端版本。",
      manifest.revision,
      currentManifest?.revision ?? null,
    );
  }
  if (payloads.length !== manifest.included_paths.length) {
    throw new Error("可移植包文件数量与 manifest 不一致。");
  }
  const entryByPath = new Map(manifest.files.map((entry) => [entry.path, entry]));
  for (let index = 0; index < manifest.included_paths.length; index += 1) {
    const path = manifest.included_paths[index];
    const payload = payloads[index];
    const entry = entryByPath.get(path);
    if (!(payload instanceof File) || !entry) {
      throw new Error(`可移植包缺少文件：${path}`);
    }
    await writeVerifiedFile(paperDirectory, path, payload, entry);
  }
  for (const path of manifest.deleted_paths) {
    await removePortablePath(paperDirectory, path);
  }
  if (manifest.bundle_type === "full" && currentManifest) {
    const nextPaths = new Set(manifest.files.map((entry) => entry.path));
    for (const entry of currentManifest.files) {
      if (!nextPaths.has(entry.path)) await removePortablePath(paperDirectory, entry.path);
    }
  }
  await writeJsonFile(paperDirectory, "manifest.json", manifest);
  const saved = await readLocalManifest(paperDirectory);
  if (!saved || saved.revision !== manifest.revision) {
    throw new Error("本地 manifest 写入后校验失败。");
  }
  await acknowledgePortableCache(paperId, manifest.revision);
  return manifest;
}

export async function syncWorkspaceToLocal(
  paperIds: string[],
  collections: LocalWorkspaceCollection[],
  onProgress?: (completed: number, total: number, paperId: string) => void,
): Promise<BulkPaperMigrationResult> {
  const uniquePaperIds = [...new Set(paperIds.map((value) => value.trim()).filter(Boolean))];
  if (!uniquePaperIds.length || uniquePaperIds.length > MAX_WORKSPACE_PAPERS) {
    throw new Error("待迁移论文数量不合法。");
  }
  const items: BulkPaperMigrationItem[] = [];
  const manifests: PortableManifest[] = [];
  for (const paperId of uniquePaperIds) {
    try {
      manifests.push(await syncPaperToLocal(paperId, { forceFull: true }));
      items.push({ paper_id: paperId, status: "done" });
    } catch (error) {
      items.push({
        paper_id: paperId,
        status: "error",
        message: (error as Error).message,
      });
    }
    onProgress?.(items.length, uniquePaperIds.length, paperId);
  }
  const failed = items.filter((item) => item.status === "error").length;
  const workspace =
    failed === 0
      ? await writeLocalWorkspaceManifest(manifests, collections)
      : null;
  return {
    items,
    completed: items.length - failed,
    failed,
    workspace,
  };
}

export async function restorePaperFromLocal(
  paperId: string,
  conflictPolicy: PortableConflictPolicy = "reject",
): Promise<{ arxiv_id: string; revision: string; status: string }> {
  const root = await requireWritableRoot();
  const paperDirectory = await root.getDirectoryHandle(encodeURIComponent(paperId));
  const manifest = await readLocalManifest(paperDirectory);
  if (!manifest || manifest.paper_id !== paperId) {
    throw new Error("本地目录中没有这篇论文的有效 manifest。");
  }
  validateManifest(manifest);
  const entries = [...manifest.files];
  const files: File[] = [];
  for (const entry of entries) {
    const file = await readPortableFile(paperDirectory, entry.path);
    await verifyFile(file, entry);
    files.push(file);
  }
  const fullManifest: PortableManifest = {
    ...manifest,
    revision: "",
    base_revision: manifest.revision,
    bundle_type: "full",
    included_paths: entries.map((entry) => entry.path),
    deleted_paths: [],
  };
  const body = new FormData();
  body.append(
    "manifest",
    new Blob([JSON.stringify(fullManifest)], { type: "application/json" }),
    "manifest.json",
  );
  files.forEach((file, index) => {
    body.append("file", file, `${index}.bin`);
  });
  body.append("conflict_policy", conflictPolicy);
  const response = await fetch(`${API_BASE}/papers/portable-bundle`, {
    method: "POST",
    body,
  });
  if (response.status === 409) {
    const payload = await response.json().catch(() => ({}));
    const detail = payload.detail ?? {};
    throw new PortableRevisionConflictError(
      detail.message || "本地副本和服务端都已变化。",
      detail.current_revision ?? null,
      detail.base_revision ?? null,
    );
  }
  if (!response.ok) {
    throw new Error(await responseMessage(response, "恢复本地论文失败"));
  }
  const restored = await response.json();
  const updated: PortableManifest = {
    ...manifest,
    revision: restored.revision,
    base_revision: restored.revision,
  };
  await writeJsonFile(paperDirectory, "manifest.json", updated);
  await acknowledgePortableCache(paperId, restored.revision);
  return restored;
}

export async function restoreWorkspaceFromLocal(
  onProgress?: (completed: number, total: number, paperId: string) => void,
): Promise<BulkPaperMigrationResult> {
  const root = await requireWritableRoot();
  const workspace = await readLocalWorkspaceManifest(root);
  const paperIds = workspace
    ? workspace.papers.map((entry) => entry.paper_id)
    : await scanLocalPaperIds(root);
  if (!paperIds.length) {
    throw new Error("所选目录中没有可恢复的陪你读论文。");
  }
  const items: BulkPaperMigrationItem[] = [];
  const expectedRevisions = new Map(
    workspace?.papers.map((entry) => [entry.paper_id, entry.revision]) ?? [],
  );
  for (const paperId of paperIds) {
    try {
      if (workspace) {
        const directory = await root.getDirectoryHandle(encodeURIComponent(paperId));
        const manifest = await readLocalManifest(directory);
        if (!manifest || manifest.revision !== expectedRevisions.get(paperId)) {
          throw new Error("论文 manifest 与工作区 revision 不一致。");
        }
      }
      await restorePaperFromLocal(paperId, "reject");
      items.push({ paper_id: paperId, status: "done" });
    } catch (error) {
      items.push({
        paper_id: paperId,
        status: "error",
        message: (error as Error).message,
      });
    }
    onProgress?.(items.length, paperIds.length, paperId);
  }
  const failed = items.filter((item) => item.status === "error").length;
  return {
    items,
    completed: items.length - failed,
    failed,
    workspace,
  };
}

export async function recoverPaperFromLocalIfAvailable(
  paperId: string,
): Promise<boolean> {
  const state = await getLocalLibraryState();
  if (state.mode !== "local_folder") return false;
  if (state.permission !== "granted") {
    throw new LocalLibraryPermissionRequiredError();
  }
  await restorePaperFromLocal(paperId, "reject");
  return true;
}

export async function renewPortableCacheLease(paperId: string): Promise<boolean> {
  try {
    const response = await fetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/portable-bundle/lease`,
      { method: "POST" },
    );
    return response.ok;
  } catch {
    return false;
  }
}

async function acknowledgePortableCache(
  paperId: string,
  revision: string,
): Promise<boolean> {
  try {
    const response = await fetch(
      `${API_BASE}/papers/${encodeURIComponent(paperId)}/portable-bundle/ack`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ revision }),
      },
    );
    // Local durability is already proven by the read-back hash. If this
    // acknowledgement fails, the server simply keeps the cache indefinitely.
    return response.ok;
  } catch {
    return false;
  }
}

async function requireWritableRoot(): Promise<FileSystemDirectoryHandle> {
  const state = await getLocalLibraryState();
  if (state.mode !== "local_folder" || state.permission !== "granted") {
    throw new Error("本地文献目录权限不可用，请在设置中重新选择目录。");
  }
  const handle = await loadRootHandle();
  if (!handle) throw new Error("本地文献目录句柄已失效。");
  return handle;
}

async function writeLocalWorkspaceManifest(
  paperManifests: PortableManifest[],
  collections: LocalWorkspaceCollection[],
): Promise<LocalWorkspaceManifest> {
  const root = await requireWritableRoot();
  const papers = paperManifests
    .map((manifest) => ({
      paper_id: manifest.paper_id,
      revision: manifest.revision,
    }))
    .sort((left, right) => left.paper_id.localeCompare(right.paper_id));
  const normalizedCollections = collections
    .map((collection) => ({
      name: collection.name.trim(),
      paper_ids: [...new Set(collection.paper_ids)]
        .sort(),
    }))
    .filter((collection) => collection.name)
    .sort((left, right) => left.name.localeCompare(right.name));
  const unsigned = {
    version: 1 as const,
    saved_at: new Date().toISOString(),
    papers,
    collections: normalizedCollections,
  };
  validateWorkspaceManifest({ ...unsigned, workspace_revision: "0".repeat(64) });
  const manifest: LocalWorkspaceManifest = {
    ...unsigned,
    workspace_revision: await sha256(
      new Blob([canonicalWorkspacePayload(unsigned)], { type: "application/json" }),
    ),
  };
  await writeJsonFile(root, WORKSPACE_MANIFEST, manifest);
  const saved = await readLocalWorkspaceManifest(root);
  if (!saved || saved.workspace_revision !== manifest.workspace_revision) {
    throw new Error("本地工作区 manifest 写入后校验失败。");
  }
  return saved;
}

async function readLocalWorkspaceManifest(
  root: FileSystemDirectoryHandle,
): Promise<LocalWorkspaceManifest | null> {
  let manifest: LocalWorkspaceManifest;
  try {
    const handle = await root.getFileHandle(WORKSPACE_MANIFEST);
    manifest = JSON.parse(
      await (await handle.getFile()).text(),
    ) as LocalWorkspaceManifest;
  } catch (error) {
    if (isNotFound(error)) return null;
    throw error;
  }
  validateWorkspaceManifest(manifest);
  const { workspace_revision: _revision, ...unsigned } = manifest;
  const actual = await sha256(
    new Blob([canonicalWorkspacePayload(unsigned)], { type: "application/json" }),
  );
  if (actual !== manifest.workspace_revision) {
    throw new Error("本地工作区 manifest 校验失败。");
  }
  return manifest;
}

async function scanLocalPaperIds(
  root: FileSystemDirectoryHandle,
): Promise<string[]> {
  const paperIds: string[] = [];
  let inspected = 0;
  for await (const [name, handle] of root.entries()) {
    inspected += 1;
    if (inspected > MAX_WORKSPACE_PAPERS + 100) {
      throw new Error("本地文献目录项目过多，已停止扫描。");
    }
    if (handle.kind !== "directory") continue;
    const directory = handle as FileSystemDirectoryHandle;
    const manifest = await readLocalManifest(directory);
    if (!manifest) continue;
    validateManifest(manifest);
    if (encodeURIComponent(manifest.paper_id) !== name) {
      throw new Error(`本地论文目录名与 manifest 不一致：${name}`);
    }
    paperIds.push(manifest.paper_id);
  }
  return [...new Set(paperIds)].sort();
}

async function readLocalManifest(
  directory: FileSystemDirectoryHandle,
): Promise<PortableManifest | null> {
  try {
    const handle = await directory.getFileHandle("manifest.json");
    return JSON.parse(await (await handle.getFile()).text()) as PortableManifest;
  } catch (error) {
    if (isNotFound(error)) return null;
    throw error;
  }
}

async function writeVerifiedFile(
  directory: FileSystemDirectoryHandle,
  portablePath: string,
  payload: File,
  entry: PortableFileEntry,
): Promise<void> {
  validatePortablePath(portablePath);
  if (payload.size !== entry.size) {
    throw new Error(`文件大小不一致：${portablePath}`);
  }
  const { parent, name } = await resolveParent(directory, portablePath, true);
  const handle = await parent.getFileHandle(name, { create: true });
  const writable = await handle.createWritable();
  try {
    await writable.write(payload);
    await writable.close();
  } catch (error) {
    await writable.abort().catch(() => undefined);
    throw error;
  }
  await verifyFile(await handle.getFile(), entry);
}

async function readPortableFile(
  directory: FileSystemDirectoryHandle,
  portablePath: string,
): Promise<File> {
  const { parent, name } = await resolveParent(directory, portablePath, false);
  return (await parent.getFileHandle(name)).getFile();
}

async function verifyFile(file: File, entry: PortableFileEntry): Promise<void> {
  if (file.size !== entry.size) throw new Error(`文件大小不一致：${entry.path}`);
  const digest = await sha256(file);
  if (digest !== entry.sha256) throw new Error(`文件校验失败：${entry.path}`);
}

async function sha256(blob: Blob): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest))
    .map((value) => value.toString(16).padStart(2, "0"))
    .join("");
}

async function resolveParent(
  directory: FileSystemDirectoryHandle,
  portablePath: string,
  create: boolean,
): Promise<{ parent: FileSystemDirectoryHandle; name: string }> {
  const parts = validatePortablePath(portablePath).split("/");
  let parent = directory;
  for (const part of parts.slice(0, -1)) {
    parent = await parent.getDirectoryHandle(part, { create });
  }
  return { parent, name: parts.at(-1)! };
}

async function removePortablePath(
  directory: FileSystemDirectoryHandle,
  portablePath: string,
): Promise<void> {
  const { parent, name } = await resolveParent(directory, portablePath, false).catch(
    () => ({ parent: null, name: "" }),
  );
  if (parent) await parent.removeEntry(name).catch(() => undefined);
}

function validatePortablePath(value: string): string {
  const parts = value.split("/");
  if (
    !value ||
    value.startsWith("/") ||
    value.includes("\\") ||
    parts.some((part) => !part || part === "." || part === "..")
  ) {
    throw new Error("可移植包包含非法路径。");
  }
  return value;
}

function validateManifest(manifest: PortableManifest): void {
  if (
    manifest.version !== 1 ||
    !manifest.paper_id ||
    !Array.isArray(manifest.files) ||
    !Array.isArray(manifest.included_paths) ||
    !Array.isArray(manifest.deleted_paths) ||
    manifest.files.length === 0 ||
    manifest.files.length > MAX_FILES
  ) {
    throw new Error("可移植包 manifest 格式错误。");
  }
  let total = 0;
  const paths = new Set<string>();
  for (const entry of manifest.files) {
    validatePortablePath(entry.path);
    if (
      paths.has(entry.path) ||
      !Number.isInteger(entry.size) ||
      entry.size < 0 ||
      entry.size > MAX_FILE_BYTES ||
      !/^[0-9a-f]{64}$/.test(entry.sha256)
    ) {
      throw new Error("可移植包文件清单不合法。");
    }
    paths.add(entry.path);
    total += entry.size;
  }
  if (total > MAX_TOTAL_BYTES || total !== manifest.total_bytes) {
    throw new Error("可移植包总大小不合法。");
  }
  for (const path of [...manifest.included_paths, ...manifest.deleted_paths]) {
    validatePortablePath(path);
  }
}

function validateWorkspaceManifest(manifest: LocalWorkspaceManifest): void {
  if (
    manifest.version !== 1 ||
    typeof manifest.saved_at !== "string" ||
    !Number.isFinite(Date.parse(manifest.saved_at)) ||
    !Array.isArray(manifest.papers) ||
    !Array.isArray(manifest.collections) ||
    !/^[0-9a-f]{64}$/.test(manifest.workspace_revision) ||
    manifest.papers.length === 0 ||
    manifest.papers.length > MAX_WORKSPACE_PAPERS ||
    manifest.collections.length > MAX_WORKSPACE_COLLECTIONS
  ) {
    throw new Error("本地工作区 manifest 格式错误。");
  }
  const paperIds = new Set<string>();
  for (const paper of manifest.papers) {
    if (
      !paper ||
      typeof paper.paper_id !== "string" ||
      !paper.paper_id ||
      paper.paper_id.length > 300 ||
      paper.paper_id.includes("\\") ||
      paper.paper_id.split("/").some((part) => !part || part === "." || part === "..") ||
      !/^[0-9a-f]{64}$/.test(paper.revision) ||
      paperIds.has(paper.paper_id)
    ) {
      throw new Error("本地工作区论文清单不合法。");
    }
    paperIds.add(paper.paper_id);
  }
  const collectionNames = new Set<string>();
  for (const collection of manifest.collections) {
    if (
      !collection ||
      typeof collection.name !== "string" ||
      !collection.name.trim() ||
      collection.name !== collection.name.trim() ||
      collection.name.length > 200 ||
      collectionNames.has(collection.name) ||
      !Array.isArray(collection.paper_ids) ||
      collection.paper_ids.length > MAX_WORKSPACE_PAPERS ||
      new Set(collection.paper_ids).size !== collection.paper_ids.length ||
      collection.paper_ids.some((paperId) => !paperIds.has(paperId))
    ) {
      throw new Error("本地工作区专题清单不合法。");
    }
    collectionNames.add(collection.name);
  }
}

function canonicalWorkspacePayload(
  manifest: Omit<LocalWorkspaceManifest, "workspace_revision">,
): string {
  return JSON.stringify(manifest);
}

function parseManifestPart(value: FormDataEntryValue | null): PortableManifest {
  if (typeof value === "string") {
    return JSON.parse(value) as PortableManifest;
  }
  if (value instanceof File) {
    throw new Error("manifest 读取方式异常。");
  }
  throw new Error("可移植包缺少 manifest。");
}

async function writeJsonFile(
  directory: FileSystemDirectoryHandle,
  name: string,
  value: unknown,
): Promise<void> {
  const handle = await directory.getFileHandle(name, { create: true });
  const writable = await handle.createWritable();
  try {
    await writable.write(JSON.stringify(value, null, 2));
    await writable.close();
  } catch (error) {
    await writable.abort().catch(() => undefined);
    throw error;
  }
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = window.indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(HANDLE_STORE)) {
        request.result.createObjectStore(HANDLE_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function saveRootHandle(handle: FileSystemDirectoryHandle): Promise<void> {
  const database = await openDatabase();
  try {
    await idbRequest(
      database.transaction(HANDLE_STORE, "readwrite").objectStore(HANDLE_STORE).put(
        handle,
        ROOT_HANDLE_KEY,
      ),
    );
  } finally {
    database.close();
  }
}

async function loadRootHandle(): Promise<FileSystemDirectoryHandle | null> {
  if (!supportsLocalPaperLibrary()) return null;
  const database = await openDatabase();
  try {
    return (
      (await idbRequest(
        database.transaction(HANDLE_STORE).objectStore(HANDLE_STORE).get(ROOT_HANDLE_KEY),
      )) ?? null
    );
  } finally {
    database.close();
  }
}

function idbRequest<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function isNotFound(error: unknown): boolean {
  return error instanceof DOMException && error.name === "NotFoundError";
}

async function responseMessage(response: Response, fallback: string): Promise<string> {
  const payload = await response.json().catch(() => null);
  if (typeof payload?.detail === "string") return payload.detail;
  if (typeof payload?.detail?.message === "string") return payload.detail.message;
  return `${fallback}: ${response.status}`;
}
