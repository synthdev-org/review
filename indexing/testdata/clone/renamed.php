<?php

declare(strict_types=1);

namespace App\Billing;

use App\Contracts\Logger;
use App\Models\Invoice;
use App\Models\LineItem;

final class OrderSummary
{
    /** @var array<string, float> */
    private array $rates = [];

    public function __construct(private readonly Logger $logger)
    {
    }

    public function addRule(string $region, float $rate): void
    {
        $this->rates[$region] = $rate;
    }

    public function calculate(Invoice $invoice, array $items): float
    {
        $gross = 0.0;
        $rebate = 0.0;
        foreach ($items as $item) {
            if ($item->quantity <= 0) {
                $this->logger->warn("skipping item {$item->sku} with quantity {$item->quantity}");
                continue;
            }
            $amount = $item->unitPrice * $item->quantity;
            $gross += $amount;
            if ($item->discountPercent > 0 && $item->discountPercent < 100) {
                $rebate += ($amount * $item->discountPercent) / 100;
            }
        }
        $rate = $this->rates[$invoice->region] ?? 0.25;
        $base = max($gross - $rebate, 0);
        $tax = round($base * $rate * 100) / 100;
        if (preg_match('/^[A-Z]{2}-\d{4}$/', $invoice->reference) !== 1) {
            throw new \InvalidArgumentException('bad order id ' . $invoice->reference);
        }
        $this->logger->info("invoice {$invoice->reference} total " . ($base + $tax));

        return $base + $tax;
    }
}
