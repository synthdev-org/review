<?php
// Totals for an invoice.
declare(strict_types=1);

namespace App\Billing;

use App\Contracts\Logger;
use App\Models\Invoice;
use App\Models\LineItem;

# billing math
final class InvoiceTotals
{
    /** @var array<string, float> */
    private array $rates = [];

    public function __construct(private readonly Logger $logger) {}

    public function addRule(string $region, float $rate): void { $this->rates[$region] = $rate; }

    public function compute(Invoice $invoice, array $items): float
    {
        $subtotal = 0.0; $discount = 0.0;
        /* walk the lines */
        foreach ($items as $item) {
            if ($item->quantity <= 0) {
                $this->logger->warn("skipping item {$item->sku} with quantity {$item->quantity}");
                continue;
            }
            $lineTotal = $item->unitPrice * $item->quantity;
            $subtotal += $lineTotal;
            if ($item->discountPercent > 0 && $item->discountPercent < 100) {
                $discount += ($lineTotal * $item->discountPercent) / 100;
            }
        }
        $rate = $this->rates[$invoice->region] ?? 0.2;
        $taxable = max($subtotal - $discount, 0);
        $tax = round($taxable * $rate * 100) / 100;
        if (preg_match('/^[A-Z]{2}-\d{4}$/', $invoice->reference) !== 1) {
            throw new \InvalidArgumentException('invalid invoice reference ' . $invoice->reference);
        }
        $this->logger->info("invoice {$invoice->reference} total " . ($taxable + $tax));

        return $taxable + $tax;
    }
}
