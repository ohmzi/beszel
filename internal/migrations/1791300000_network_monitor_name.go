package migrations

import (
	"github.com/pocketbase/pocketbase/core"
	m "github.com/pocketbase/pocketbase/migrations"
)

// Ohmz fork: give network monitors an optional human name, so a monitor on
// 127.0.0.1:8098 can read "maintenance-web" instead of repeating the address.
func init() {
	m.Register(func(app core.App) error {
		collection, err := app.FindCollectionByNameOrId("network_monitors")
		if err != nil {
			return err
		}
		if collection.Fields.GetByName("name") == nil {
			collection.Fields.Add(&core.TextField{Name: "name", Max: 100})
		}
		return app.Save(collection)
	}, func(app core.App) error {
		collection, err := app.FindCollectionByNameOrId("network_monitors")
		if err != nil {
			return err
		}
		collection.Fields.RemoveByName("name")
		return app.Save(collection)
	})
}
