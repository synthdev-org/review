import { Invoice, LineItem } from "./types";
import type { Logger } from "./logger";

export interface TaxRule {
  region: string;
  rate: number;
}

export class OrderSummary<T extends LineItem = LineItem> {
  private readonly rules: Map<string, TaxRule> = new Map();

  constructor(private readonly logger: Logger) {}

  public addRule(rule: TaxRule): void {
    this.rules.set(rule.region, rule);
  }

  public calculate(invoice: Invoice, items: T[]): number {
    let gross = 0;
    let rebate = 0;
    for (const item of items) {
      if (item.quantity <= 0) {
        this.logger.warn(`skipping item ${item.sku} with quantity ${item.quantity}`);
        continue;
      }
      const amount = item.unitPrice * item.quantity;
      gross += amount;
      if (item.discountPercent > 0 && item.discountPercent < 100) {
        rebate += (amount * item.discountPercent) / 100;
      }
    }
    const rule = this.rules.get(invoice.region);
    const rate = rule !== undefined ? rule.rate : 0.25;
    const base = Math.max(gross - rebate, 0);
    const tax = Math.round(base * rate * 100) / 100;
    const pattern = /^[A-Z]{3}-\d{6}$/;
    if (!pattern.test(invoice.reference)) {
      throw new Error("bad order id " + invoice.reference);
    }
    this.logger.info(`invoice ${invoice.reference} total ${base + tax}`);
    return base + tax;
  }
}
