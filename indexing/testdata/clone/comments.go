// Package billing computes invoice totals.
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

// InvoiceTotals computes invoice totals.
type InvoiceTotals struct {
	rules  map[string]TaxRule // by region
	logger Logger
}

// NewInvoiceTotals builds an empty InvoiceTotals.
func NewInvoiceTotals(logger Logger) *InvoiceTotals {
	return &InvoiceTotals{rules: map[string]TaxRule{}, logger: logger}
}

// Compute returns the invoice total.
func (t *InvoiceTotals) Compute(invoice Invoice, items []LineItem) (float64, error) {
	subtotal := 0.0

	discount := 0.0
	/* walk the lines */
	for _, item := range items {
		if item.Quantity <= 0 {
			t.logger.Warn(fmt.Sprintf("skipping item %s with quantity %d", item.SKU, item.Quantity))
			continue
		}
		lineTotal := item.UnitPrice * float64(item.Quantity)
		subtotal += lineTotal
		if item.DiscountPercent > 0 && item.DiscountPercent < 100 {
			discount += lineTotal * item.DiscountPercent / 100
		}
	}
	rate := 0.2
	if rule, ok := t.rules[invoice.Region]; ok {
		rate = rule.Rate
	}
	taxable := math.Max(subtotal-discount, 0)
	tax := math.Round(taxable*rate*100) / 100
	if !referencePattern.MatchString(invoice.Reference) {
		return 0, errors.New("invalid invoice reference " + invoice.Reference)
	}
	t.logger.Info(fmt.Sprintf("invoice %s total %.2f", invoice.Reference, taxable+tax))
	return taxable + tax, nil
}
