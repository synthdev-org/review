// Vitest + React Testing Library setup, per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section (Unit I) — "the
// standard pairing for a Vite project." Extends Vitest's `expect` with
// jest-dom's DOM matchers (`toBeDisabled`, `toHaveTextContent`, etc.) and
// resets the DOM between tests so one screen's test can't leak state into
// the next.
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
