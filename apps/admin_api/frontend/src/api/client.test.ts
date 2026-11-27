import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, apiRequest } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("apiRequest path guard", () => {
  it.each(["users", "//evil.example/x", "/a/../b", "/a/..?x=1", "/a\\b"])(
    "rejects %s without calling fetch",
    async (path) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      await expect(apiRequest(path)).rejects.toBeInstanceOf(ApiError);
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("sends an encoded path under /api/v1", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await apiRequest(`/users/${encodeURIComponent("a/b")}/deactivate`);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/users/a%2Fb/deactivate");
  });
});
