package billing

import (
	"errors"
	"fmt"
	"math"
	"regexp"
)

var referencePattern = regexp.MustCompile(`^[A-Z]{2}-\d{4}$`)

// TaxRule is a regional rate.
type TaxRule struct {
	Region string
	Rate   float64
}

// OrderSummary computes invoice totals.
type OrderSummary struct {
	rules  map[string]TaxRule
	logger Logger
}

// NewOrderSummary builds an empty OrderSummary.
func NewOrderSummary(logger Logger) *OrderSummary {
	return &OrderSummary{rules: map[string]TaxRule{}, logger: logger}
}

// Calculate returns the invoice total.
func (t *OrderSummary) Calculate(invoice Invoice, items []LineItem) (float64, error) {
	gross := 0.0
	rebate := 0.0
	for _, item := range items {
		if item.Quantity <= 0 {
			t.logger.Warn(fmt.Sprintf("skipping item %s with quantity %d", item.SKU, item.Quantity))
			continue
		}
		amount := item.UnitPrice * float64(item.Quantity)
		gross += amount
		if item.DiscountPercent > 0 && item.DiscountPercent < 100 {
			rebate += amount * item.DiscountPercent / 100
		}
	}
	rate := 0.25
	if rule, ok := t.rules[invoice.Region]; ok {
		rate = rule.Rate
	}
	base := math.Max(gross-rebate, 0)
	tax := math.Round(base*rate*100) / 100
	if !referencePattern.MatchString(invoice.Reference) {
		return 0, errors.New("bad order id " + invoice.Reference)
	}
	t.logger.Info(fmt.Sprintf("invoice %s total %.2f", invoice.Reference, base+tax))
	return base + tax, nil
}
