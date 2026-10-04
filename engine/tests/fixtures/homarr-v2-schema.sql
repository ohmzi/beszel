-- Generated 2026-10-03. Real DDL of the Homarr v2 tables that widgets/install_homarr_widgets.py reads or writes, plus every table they reference.
-- Extracted by tests/test_homarr_installer_v2.py --regen-schema from a read-only (sqlite online backup) copy of the live database,
-- tables: board, user, search_engine, integration, app, section, layout, item, item_layout, section_layout, custom_widget_v2_definition, custom_widget_v2_secret, custom_widget_definition, custom_widget_secret.
-- Legacy tables custom_widget_definition / custom_widget_secret are kept: the installer must never write them.
CREATE TABLE `board` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`is_public` integer DEFAULT false NOT NULL,
	`creator_id` text,
	`page_title` text,
	`meta_title` text,
	`logo_image_url` text,
	`favicon_image_url` text,
	`background_image_url` text,
	`background_image_attachment` text DEFAULT 'fixed' NOT NULL,
	`background_image_repeat` text DEFAULT 'no-repeat' NOT NULL,
	`background_image_size` text DEFAULT 'cover' NOT NULL,
	`primary_color` text DEFAULT '#fa5252' NOT NULL,
	`secondary_color` text DEFAULT '#fd7e14' NOT NULL,
	`opacity` integer DEFAULT 100 NOT NULL,
	`custom_css` text,
	`disable_status` integer DEFAULT false NOT NULL, `item_radius` text DEFAULT 'lg' NOT NULL, `icon_color` text,
	FOREIGN KEY (`creator_id`) REFERENCES "user"(`id`) ON UPDATE no action ON DELETE set null
);
CREATE UNIQUE INDEX `board_name_unique` ON `board` (`name`);
CREATE TABLE "user" (
	`id` text PRIMARY KEY NOT NULL,
	`name` text,
	`email` text,
	`email_verified` integer,
	`image` text,
	`password` text,
	`provider` text DEFAULT 'credentials' NOT NULL,
	`home_board_id` text,
    `mobile_home_board_id` text,
    `default_search_engine_id` text,
    `open_search_in_new_tab` integer DEFAULT true NOT NULL,
	`color_scheme` text DEFAULT 'dark' NOT NULL,
	`first_day_of_week` integer DEFAULT 1 NOT NULL,
	`ping_icons_enabled` integer DEFAULT false NOT NULL, `ddg_bangs` integer DEFAULT true NOT NULL, `completed_manage_tour` integer DEFAULT false NOT NULL, `completed_board_tour` integer DEFAULT false NOT NULL, `enable_right_click_on_widgets` integer DEFAULT true NOT NULL, `header_preferences` text DEFAULT '{"version":3,"visible":true,"searchDisplay":"input","logoDisplay":"logoAndText","zones":{"left":[{"type":"builtin","id":"logo"}],"center":[{"type":"builtin","id":"search"}],"right":[{"type":"builtin","id":"boardEdit"},{"type":"builtin","id":"boardSettings"},{"type":"builtin","id":"user"}]}}' NOT NULL, `byte_unit_system` text DEFAULT 'decimal' NOT NULL,
	FOREIGN KEY (`home_board_id`) REFERENCES `board`(`id`) ON UPDATE no action ON DELETE set null,
    FOREIGN KEY (`mobile_home_board_id`) REFERENCES `board`(`id`) ON UPDATE no action ON DELETE set null,
    FOREIGN KEY (`default_search_engine_id`) REFERENCES `search_engine`(`id`) ON UPDATE no action ON DELETE set null
);
CREATE TABLE "search_engine" (
	`id` text PRIMARY KEY NOT NULL,
	`icon_url` text NOT NULL,
	`name` text NOT NULL,
	`short` text NOT NULL,
	`description` text,
	`url_template` text,
	`type` text DEFAULT 'generic' NOT NULL,
	`integration_id` text,
	FOREIGN KEY (`integration_id`) REFERENCES `integration`(`id`) ON UPDATE no action ON DELETE cascade
);
CREATE UNIQUE INDEX `search_engine_short_unique` ON `search_engine` (`short`);
CREATE TABLE `integration` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`url` text NOT NULL,
	`kind` text NOT NULL
, `app_id` text REFERENCES app(id));
CREATE INDEX `integration__kind_idx` ON `integration` (`kind`);
CREATE TABLE `app` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`description` text,
	`icon_url` text NOT NULL,
	`href` text
, `ping_url` text);
CREATE TABLE "section" (
	`id` text PRIMARY KEY NOT NULL,
	`board_id` text NOT NULL,
	`kind` text NOT NULL,
	`x_offset` integer,
	`y_offset` integer,
	`name` text, `options` text DEFAULT '{"json": {}}',
	FOREIGN KEY (`board_id`) REFERENCES `board`(`id`) ON UPDATE no action ON DELETE cascade
);
CREATE TABLE `layout` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`board_id` text NOT NULL,
	`column_count` integer NOT NULL,
	`breakpoint` integer DEFAULT 0 NOT NULL, `left_gutter_column_count` integer DEFAULT 0 NOT NULL, `right_gutter_column_count` integer DEFAULT 0 NOT NULL, `role` text DEFAULT 'custom' NOT NULL,
	FOREIGN KEY (`board_id`) REFERENCES `board`(`id`) ON UPDATE no action ON DELETE cascade
);
CREATE TABLE "item" (
	`id` text PRIMARY KEY NOT NULL,
	`board_id` text NOT NULL,
	`kind` text NOT NULL,
	`options` text DEFAULT '{"json": {}}' NOT NULL,
	`advanced_options` text DEFAULT '{"json": {}}' NOT NULL,
	FOREIGN KEY (`board_id`) REFERENCES `board`(`id`) ON UPDATE no action ON DELETE cascade
);
CREATE TABLE `item_layout` (
	`item_id` text NOT NULL,
	`section_id` text NOT NULL,
	`layout_id` text NOT NULL,
	`x_offset` integer NOT NULL,
	`y_offset` integer NOT NULL,
	`width` integer NOT NULL,
	`height` integer NOT NULL,
	PRIMARY KEY(`item_id`, `section_id`, `layout_id`),
	FOREIGN KEY (`item_id`) REFERENCES `item`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`section_id`) REFERENCES `section`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`layout_id`) REFERENCES `layout`(`id`) ON UPDATE no action ON DELETE cascade
);
CREATE TABLE `section_layout` (
	`section_id` text NOT NULL,
	`layout_id` text NOT NULL,
	`parent_section_id` text,
	`x_offset` integer NOT NULL,
	`y_offset` integer NOT NULL,
	`width` integer NOT NULL,
	`height` integer NOT NULL,
	PRIMARY KEY(`section_id`, `layout_id`),
	FOREIGN KEY (`section_id`) REFERENCES `section`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`layout_id`) REFERENCES `layout`(`id`) ON UPDATE no action ON DELETE cascade,
	FOREIGN KEY (`parent_section_id`) REFERENCES `section`(`id`) ON UPDATE no action ON DELETE cascade
);
CREATE TABLE `custom_widget_v2_definition` (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`description` text,
	`icon_url` text,
	`sources` text NOT NULL,
	`requests` text NOT NULL,
	`options` text NOT NULL,
	`template` text NOT NULL,
	`enabled` integer DEFAULT true NOT NULL,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`updated_at` integer DEFAULT (unixepoch()) NOT NULL,
	`creator_id` text,
	FOREIGN KEY (`creator_id`) REFERENCES `user`(`id`) ON UPDATE no action ON DELETE set null
);
CREATE TABLE `custom_widget_v2_secret` (
	`source_id` text NOT NULL,
	`kind` text NOT NULL,
	`encrypted_value` text NOT NULL,
	`updated_at` integer NOT NULL,
	`definition_id` text NOT NULL,
	PRIMARY KEY(`definition_id`, `source_id`, `kind`),
	FOREIGN KEY (`definition_id`) REFERENCES `custom_widget_v2_definition`(`id`) ON UPDATE no action ON DELETE cascade
);
CREATE TABLE "custom_widget_definition" (
	`id` text PRIMARY KEY NOT NULL,
	`name` text NOT NULL,
	`description` text,
	`icon_url` text,
	`url` text NOT NULL,
	`auth_type` text DEFAULT 'none' NOT NULL,
	`header_name` text,
	`method` text DEFAULT 'GET' NOT NULL,
	`request_body` text,
	`display_type` text DEFAULT 'singleValue' NOT NULL,
	`display_config` text DEFAULT '{"json": {}}' NOT NULL,
	`enabled` integer DEFAULT true NOT NULL,
	`created_at` integer DEFAULT (unixepoch()) NOT NULL,
	`updated_at` integer DEFAULT (unixepoch()) NOT NULL,
	`creator_id` text,
	FOREIGN KEY (`creator_id`) REFERENCES `user`(`id`) ON UPDATE no action ON DELETE set null
);
CREATE TABLE `custom_widget_secret` (
	`kind` text NOT NULL,
	`value` text NOT NULL,
	`updated_at` integer NOT NULL,
	`definition_id` text NOT NULL,
	PRIMARY KEY(`definition_id`, `kind`),
	FOREIGN KEY (`definition_id`) REFERENCES `custom_widget_definition`(`id`) ON UPDATE no action ON DELETE cascade
);
