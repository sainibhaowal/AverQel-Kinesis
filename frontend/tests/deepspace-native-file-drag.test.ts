import {
  MAX_NATIVE_DRAG_BYTES,
  readBoundedResponseBytes,
  stageNativeFileForDrag,
  type NativeFileDragApi,
} from "@/app/dashboard/deepspace/_lib/native-file-drag";

function responseWithBytes(bytes: Uint8Array, contentType: string | null = "application/pdf"): Response {
  return {
    ok: true,
    headers: { get: () => contentType },
    body: null,
    arrayBuffer: async () => bytes.slice().buffer,
  } as unknown as Response;
}

function streamResponse(reader: {
  read: ReturnType<typeof vi.fn>;
  cancel: ReturnType<typeof vi.fn>;
  releaseLock: ReturnType<typeof vi.fn>;
}): Response {
  return {
    ok: true,
    headers: { get: () => "application/octet-stream" },
    body: { getReader: () => reader },
  } as unknown as Response;
}

function makeApi(): NativeFileDragApi {
  return {
    prepare: vi.fn(async () => ({ handle: "opaque-handle", name: "quarterly.pdf" })),
    start: vi.fn(async () => true),
    cancel: vi.fn(async () => true),
    release: vi.fn(async () => true),
  };
}

const file = {
  id: "file-7",
  name: "quarterly.pdf",
  content_type: "application/pdf",
  size_bytes: 4,
};

describe("native Library drag staging", () => {
  it("reads a bodyless response within the byte bound", async () => {
    await expect(readBoundedResponseBytes(responseWithBytes(new Uint8Array([1, 2])), 2)).resolves.toEqual(
      new Uint8Array([1, 2]),
    );
    await expect(readBoundedResponseBytes(responseWithBytes(new Uint8Array([1, 2, 3])), 2)).rejects.toThrow(
      /too large/,
    );
  });

  it("joins response stream chunks and releases its reader", async () => {
    const reader = {
      read: vi
        .fn()
        .mockResolvedValueOnce({ done: false, value: new Uint8Array([1, 2]) })
        .mockResolvedValueOnce({ done: false, value: new Uint8Array([3]) })
        .mockResolvedValueOnce({ done: true, value: undefined }),
      cancel: vi.fn(async () => undefined),
      releaseLock: vi.fn(),
    };

    await expect(readBoundedResponseBytes(streamResponse(reader), 3)).resolves.toEqual(
      new Uint8Array([1, 2, 3]),
    );
    expect(reader.releaseLock).toHaveBeenCalledOnce();
  });

  it("cancels an oversized response stream and releases its reader", async () => {
    const reader = {
      read: vi.fn().mockResolvedValue({ done: false, value: new Uint8Array([1, 2, 3]) }),
      cancel: vi.fn(async () => undefined),
      releaseLock: vi.fn(),
    };

    await expect(readBoundedResponseBytes(streamResponse(reader), 2)).rejects.toThrow(/too large/);
    expect(reader.cancel).toHaveBeenCalledOnce();
    expect(reader.releaseLock).toHaveBeenCalledOnce();
  });

  it("fetches through the authenticated Library endpoint and stages bytes in Electron", async () => {
    const api = makeApi();
    const fetcher = vi.fn(async () => responseWithBytes(new Uint8Array([37, 80, 68, 70])));

    await expect(
      stageNativeFileForDrag({
        file,
        conversationId: "conversation-1",
        api,
        fetcher,
        isCancelled: () => false,
      }),
    ).resolves.toBe("opaque-handle");

    expect(fetcher).toHaveBeenCalledWith(
      "/deepspace/library/conversation-1/files/file-7/content?download=true",
      { timeoutMs: 120_000 },
    );
    expect(api.prepare).toHaveBeenCalledWith({
      name: file.name,
      contentType: "application/pdf",
      bytes: new Uint8Array([37, 80, 68, 70]),
    });
    expect(api.cancel).not.toHaveBeenCalled();
  });

  it("rejects oversized files, missing conversations, and unauthorized downloads", async () => {
    const api = makeApi();
    const fetcher = vi.fn(async () => responseWithBytes(new Uint8Array([1])));

    await expect(
      stageNativeFileForDrag({
        file: { ...file, size_bytes: MAX_NATIVE_DRAG_BYTES + 1 },
        conversationId: "conversation-1",
        api,
        fetcher,
        isCancelled: () => false,
      }),
    ).rejects.toThrow(/32 MiB/);
    await expect(
      stageNativeFileForDrag({ file, conversationId: null, api, fetcher, isCancelled: () => false }),
    ).rejects.toThrow(/conversation/);
    await expect(
      stageNativeFileForDrag({
        file,
        conversationId: "conversation-1",
        api,
        fetcher: async () => ({ ...responseWithBytes(new Uint8Array([1])), ok: false }),
        isCancelled: () => false,
      }),
    ).rejects.toThrow(/authorized Library download failed/);
    expect(fetcher).not.toHaveBeenCalled();
  });

  it("does not stage a cancelled request and cancels a handle if cancellation races staging", async () => {
    const api = makeApi();
    const fetcher = vi.fn(async () => responseWithBytes(new Uint8Array([1])));
    const options = { file, conversationId: "conversation-1", api, fetcher };

    await expect(stageNativeFileForDrag({ ...options, isCancelled: () => true })).resolves.toBeNull();
    expect(api.prepare).not.toHaveBeenCalled();

    let checks = 0;
    await expect(
      stageNativeFileForDrag({
        ...options,
        isCancelled: () => {
          checks += 1;
          return checks > 1;
        },
      }),
    ).resolves.toBeNull();
    expect(api.cancel).toHaveBeenCalledWith("opaque-handle");
  });

  it("uses the file content type when the response omits one", async () => {
    const api = makeApi();
    await stageNativeFileForDrag({
      file,
      conversationId: "conversation-1",
      api,
      fetcher: async () => responseWithBytes(new Uint8Array([1]), null),
      isCancelled: () => false,
    });
    expect(api.prepare).toHaveBeenCalledWith(expect.objectContaining({ contentType: file.content_type }));
  });
});
