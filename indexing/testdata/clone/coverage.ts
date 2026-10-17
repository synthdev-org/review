#!/usr/bin/env node
type Pair<A, B> = [A, B];
enum Color { Red, Green = "green", Blue = 4 }
namespace Geometry { export const pi = 3.14; }
declare module "ext" { export function f(x: number): string; }
abstract class Shape { abstract area(): number; static readonly sides: number = 0; #secret = 1; }
function* gen<T>(items: readonly T[]): Generator<T, void, undefined> { for (const i of items) yield i; }
async function load(url: string): Promise<unknown> { const r = await fetch(url); return r.json(); }
const obj = { a, b, ["k" + 1]: 2, ...rest, method() { return this; }, get x() { return super.x; } };
const { a: renamed, ...others } = obj as unknown as Record<string, number>;
label: for (let i = 0; i < 3; i++) { if (i === 1) continue label; else break label; }
const t = `tpl ${a} and ${b + 1}`; const u = undefined ?? null; const big = 10n; const hex = 0xff;
type Cond<T> = T extends string ? "s" : T extends number ? "n" : never;
type Mapped = { readonly [K in keyof Obj]?: Obj[K] };
let tuple: [x: number, y?: string, ...rest: boolean[]] = [1];
export default class {}
export * from "./other";
export { a as b };
try { risky(); } catch (e) { handle(e as Error); } finally { done(); }
const re = /ab+c/gi; const neg = !flag && (x as any)!;
if (typeof x === "undefined" || x instanceof Date || "k" in obj) { delete obj.k; void 0; }
switch (n) { case 1: break; default: throw new TypeError("no"); }
do { n--; } while (n > 0);
let fnType: (a: number) => void = (a) => {}; let u2: unique symbol; let ob: object = {};
@decorator class Decorated { @prop() field = 1; constructor(@inject() private s: string) {} }
type TplA = `abc`; type TplB = `a${string}b`;
