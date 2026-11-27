// One shared data-fetching hook every screen uses, so every screen
// that calls the backend has an explicit loading, empty, error, and
// populated-success state. Centralizing this here means every screen's
// four states come from the same, once-tested state machine, and a 401
// (US-A2's idle expiry / a revoked session) is handled identically
// everywhere rather than reimplemented per screen.
import { useCallback, useEffect, useState } from "react";

import { ApiError, isSessionExpiredError } from "../api/client";
import { useAuth } from "../auth/AuthContext";

export type AsyncState<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "success"; data: T };

export function useAsyncData<T>(
  fetcher: () => Promise<T>,
  deps: readonly unknown[] = []
): { state: AsyncState<T>; reload: () => void } {
  const { handleSessionExpired } = useAuth();
  const [reloadToken, setReloadToken] = useState(0);
  const [state, setState] = useState<AsyncState<T>>({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetcher()
      .then((data) => {
        if (!cancelled) setState({ status: "success", data });
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        if (isSessionExpiredError(error)) {
          handleSessionExpired();
          return;
        }
        const message = error instanceof ApiError ? error.detail : "something went wrong";
        setState({ status: "error", message });
      });
    return () => {
      cancelled = true;
    };
    // `fetcher`/`handleSessionExpired` are intentionally excluded: `fetcher`
    // is re-created on every render by the caller (a fresh closure over its
    // own params), and `deps` names the actual values that should trigger a
    // re-fetch — including `fetcher` here would re-run this effect every
    // render regardless of whether its underlying params changed.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadToken, ...deps]);

  const reload = useCallback(() => { setReloadToken((token) => token + 1); }, []);

  return { state, reload };
}
