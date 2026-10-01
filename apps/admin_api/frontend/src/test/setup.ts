// Vitest + React Testing Library setup, per
// docs/plans/plan-deliver-admin-gui.md's Phase 8 section (Unit I) — "the
// standard pairing for a Vite project." Extends Vitest's `expect` with
// jest-dom's DOM matchers (`toBeDisabled`, `toHaveTextContent`, etc.) and
// resets the DOM between tests so one screen's test can't leak state into
// the next.
import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { expect } from "vitest";
import { afterEach } from "vitest";
import * as axeMatchers from "vitest-axe/matchers";

// WCAG 2.1 AA baseline (Phase 8's UX requirement) is otherwise only
// verified by manual code reading — this gives each screen's test an
// automated `toHaveNoViolations()` assertion backing that claim.
expect.extend(axeMatchers);

afterEach(() => {
  cleanup();
});
