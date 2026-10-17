package demo

import "fmt"

type Number interface{ ~int | ~float64 }

type Stack[T any] struct{ items []T }

func (s *Stack[T]) Push(v T) { s.items = append(s.items, v) }

func Map[T, U any](in []T, f func(T) U) []U {
	out := make([]U, 0, len(in))
	for _, v := range in {
		out = append(out, f(v))
	}
	return out
}

type Shape interface{ Area() float64 }

const (
	A = iota
	B
	big = 1 << 40
)

var (
	r  = 'x'
	im = 2i
	f  = 1.5e3
	h  = 0xFF
	s  = "interp\n" + `raw`
)

func run(ch chan<- int, done <-chan struct{}) {
outer:
	for i := 0; i < 3; i++ {
		select {
		case ch <- i:
		case <-done:
			break outer
		default:
			continue outer
		}
	}
	switch v := interface{}(nil).(type) {
	case nil:
		fmt.Println("nil", v)
	case int, string:
	}
	defer func() { _ = recover() }()
	go func() {}()
	var arr [3]int
	m := map[string][]int{"a": {1}}
	_, _ = arr, m
	x := &struct{ P *int }{}
	_ = x.P
	if y := 1; y > 0 {
		goto end
	}
end:
	fmt.Println(true, false, nil)
}
