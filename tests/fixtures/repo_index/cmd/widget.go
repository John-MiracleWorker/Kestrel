package widget

import "fmt"

type GoWidget struct{}

func BuildWidget(name string) string {
	return fmt.Sprintf("widget:%s", name)
}
