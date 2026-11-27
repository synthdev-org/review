import { useCallback, useState } from "react";

import type { NoticeValue } from "../components/Notice";

export function useNotice(): {
  notice: NoticeValue | null;
  showSuccess: (message: string) => void;
  showError: (message: string) => void;
  dismiss: () => void;
} {
  const [notice, setNotice] = useState<NoticeValue | null>(null);

  const showSuccess = useCallback((message: string) => { setNotice({ kind: "success", message }); }, []);
  const showError = useCallback((message: string) => { setNotice({ kind: "error", message }); }, []);
  const dismiss = useCallback(() => { setNotice(null); }, []);

  return { notice, showSuccess, showError, dismiss };
}
