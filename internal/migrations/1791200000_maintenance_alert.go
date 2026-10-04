package migrations

import (
	"github.com/pocketbase/pocketbase/core"
	m "github.com/pocketbase/pocketbase/migrations"
)

// Ohmz fork: allow a "Maintenance" alert on the homelab-maint verdict
// (0 healthy, 1 attention, 2 critical) reported by the maintenance collector.
func init() {
	m.Register(func(app core.App) error {
		collection, err := app.FindCollectionByNameOrId("alerts")
		if err != nil {
			return err
		}
		field, ok := collection.Fields.GetByName("name").(*core.SelectField)
		if !ok || field == nil {
			return nil
		}
		for _, v := range field.Values {
			if v == "Maintenance" {
				return nil
			}
		}
		field.Values = append(field.Values, "Maintenance")
		return app.Save(collection)
	}, func(app core.App) error {
		collection, err := app.FindCollectionByNameOrId("alerts")
		if err != nil {
			return err
		}
		field, ok := collection.Fields.GetByName("name").(*core.SelectField)
		if !ok || field == nil {
			return nil
		}
		values := field.Values[:0]
		for _, v := range field.Values {
			if v != "Maintenance" {
				values = append(values, v)
			}
		}
		field.Values = values
		return app.Save(collection)
	})
}
