// vitest-axe ships its type augmentation against the old `declare global {
// namespace Vi { ... } }` convention, which vitest 2.x no longer reads (it
// now merges via `declare module "vitest"`, the same convention
// @testing-library/jest-dom/vitest.d.ts already uses here) — so its own
// `vitest-axe/extend-expect` types silently no-op. This re-declares the one
// matcher we actually use against vitest's current convention instead.
import type { AxeMatchers } from "vitest-axe/matchers";

declare module "vitest" {
  interface Assertion<T = any> extends AxeMatchers {}
  interface AsymmetricMatchersContaining extends AxeMatchers {}
}
