export const MAX_NATIVE_DRAG_BYTES = 32 * 1024 * 1024;

export type NativeFileDragApi = {
  prepare: (input: { name: string; contentType: string; bytes: Uint8Array }) => Promise<{ handle: string; name: string }>;
  start: (handle: string) => Promise<boolean>;
  cancel: (handle: string) => Promise<boolean>;
  release: (handle: string) => Promise<boolean>;
};

export type NativeDragFile = {
  id: string;
  name: string;
  content_type: string;
  size_bytes: number;
};

type AuthenticatedFetcher = (path: string, options: { timeoutMs: number }) => Promise<unknown>;

export async function readBoundedResponseBytes(response: Response, maxBytes: number): Promise<Uint8Array> {
  if (!response.body) {
    const bytes = new Uint8Array(await response.arrayBuffer());
    if (bytes.byteLength > maxBytes) throw new Error("The file is too large for native drag-out.");
    return bytes;
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let byteLength = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      byteLength += value.byteLength;
      if (byteLength > maxBytes) {
        await reader.cancel();
        throw new Error("The file is too large for native drag-out.");
      }
      chunks.push(value);
    }
  } finally {
    reader.releaseLock();
  }
  const bytes = new Uint8Array(byteLength);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return bytes;
}

export async function stageNativeFileForDrag({
  file,
  conversationId,
  api,
  fetcher,
  isCancelled,
}: {
  file: NativeDragFile;
  conversationId: string | null;
  api: Pick<NativeFileDragApi, "prepare" | "cancel">;
  fetcher: AuthenticatedFetcher;
  isCancelled: () => boolean;
}): Promise<string | null> {
  if (file.size_bytes > MAX_NATIVE_DRAG_BYTES) {
    throw new Error("Native drag-out is limited to files up to 32 MiB. Use Download for larger files.");
  }
  if (!conversationId) throw new Error("The conversation is no longer available.");

  const response = (await fetcher(
    `/deepspace/library/${conversationId}/files/${file.id}/content?download=true`,
    { timeoutMs: 120_000 },
  )) as Response;
  if (!response.ok) throw new Error("The authorized Library download failed.");
  const bytes = await readBoundedResponseBytes(response, MAX_NATIVE_DRAG_BYTES);
  if (isCancelled()) return null;

  const prepared = await api.prepare({
    name: file.name,
    contentType: response.headers.get("content-type") || file.content_type,
    bytes,
  });
  if (isCancelled()) {
    await api.cancel(prepared.handle);
    return null;
  }
  return prepared.handle;
}
