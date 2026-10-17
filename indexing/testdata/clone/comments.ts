// Totals for an invoice.
import { Invoice, LineItem } from "./types";
import type { Logger } from "./logger";

/** A tax rule. */
export interface TaxRule {
  region: string;
  rate: number;
}


export class InvoiceTotals<T extends LineItem = LineItem> {
  private readonly rules: Map<string, TaxRule> = new Map(); // by region

  constructor(private readonly logger: Logger) {}

  public addRule(rule: TaxRule): void { this.rules.set(rule.region, rule); }

  public compute(invoice: Invoice, items: T[]): number {
    let subtotal = 0; let discount = 0;
    /* walk the lines */
    for (const item of items) {
      if (item.quantity <= 0) {
        this.logger.warn(`skipping item ${item.sku} with quantity ${item.quantity}`);
        continue;
      }
      const lineTotal = item.unitPrice * item.quantity;
      subtotal += lineTotal;
      if (item.discountPercent > 0 && item.discountPercent < 100) {
        discount += (lineTotal * item.discountPercent) / 100;
      }
    }
    const rule = this.rules.get(invoice.region);
    const rate = rule !== undefined ? rule.rate : 0.2;
    const taxable = Math.max(subtotal - discount, 0);
    const tax = Math.round(taxable * rate * 100) / 100;
    const pattern = /^[A-Z]{2}-\d{4}$/;
    if (!pattern.test(invoice.reference)) {
      throw new Error("invalid invoice reference " + invoice.reference);
    }
    this.logger.info(`invoice ${invoice.reference} total ${taxable + tax}`);
    return taxable + tax;
  }
}
