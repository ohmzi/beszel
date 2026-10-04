// Browser entry for `render-check.mjs --shot`: bundled by esbuild together with the REAL Homarr custom-widget runtime and drawn
// in headless Chromium so charts really render and light/dark/size can be inspected. It rebuilds the tile the way a board does:
// <Card p=0 container-type:size overflow auto> (packages/widgets/src/widget-card-shell.tsx) around <CustomJsxRenderer>.
// "@homarr-repo" is an esbuild alias for the Homarr checkout (HOMARR_REPO); the harness passes it, nothing here is hard-coded.
import "@mantine/core/styles.css";
import "@mantine/charts/styles.css";

import { createRoot } from "react-dom/client";
import { Card, MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { createCustomJsxBindings, createCustomJsxComponents } from "@homarr-repo/packages/custom-widgets/src/jsx/index.ts";
import {
  CustomJsxRenderer,
  CustomWidgetRuntimeProvider,
  parseRequestCapabilities,
} from "@homarr-repo/packages/custom-widgets/src/runtime/index.ts";
import { SafeTablerIcon } from "@homarr-repo/packages/widgets/src/custom-api/jsx-icon-adapter.tsx";
import { theme } from "@homarr-repo/packages/ui/src/theme.ts";

declare global {
  interface Window {
    __PREVIEW__: {
      template: string;
      data: Record<string, unknown>;
      status: Record<string, unknown>;
      options: Record<string, unknown>;
      requests: unknown[];
      scheme: "dark" | "light";
      width: number;
      height: number;
      opacity: number;
    };
  }
}

const cfg = window.__PREVIEW__;
const components = createCustomJsxComponents({
  TablerIcon: SafeTablerIcon as never,
  copyLabels: { copy: "Copy", copied: "Copied" },
});
const messages = {
  requestIdRequired: "This network control requires a named requestId.",
  unsavedPreview: "Run the initial request test in the editor before testing named requests.",
  invalidParams: "The request parameters are invalid.",
  loadRequest: "Load request",
  requestFailed: "The request failed.",
  loading: "Loading request...",
  retry: "Retry request",
  widgetItemUnavailable: "This widget item is not available.",
  actionsDisabledEditMode: "Actions are disabled while the board is in edit mode.",
  actionSimulated: "Action simulated; no request was sent.",
  actionCompleted: "Action completed successfully.",
  confirmDelete: "Confirm this destructive action.",
  toggle: "Toggle",
  refresh: "Refresh",
};
// no network: every query answers from the fixtures the harness embedded in the page
const port = {
  query: async ({ requestId }: { requestId: string }) => ({ ok: true, status: 200, data: cfg.data[requestId] ?? null }),
  executeAction: async () => ({ ok: true, status: 200, data: null, simulated: true }),
  invalidate: async () => undefined,
  confirm: async () => true,
  notify: () => undefined,
};
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

createRoot(document.getElementById("root") as HTMLElement).render(
  <MantineProvider theme={theme} defaultColorScheme={cfg.scheme} forceColorScheme={cfg.scheme}>
    <QueryClientProvider client={queryClient}>
      <CustomWidgetRuntimeProvider
        itemId="harness-item"
        isEditMode={false}
        requestCapabilities={parseRequestCapabilities(cfg.requests)}
        port={port as never}
        messages={messages}
      >
        <Card
          w={cfg.width}
          h={cfg.height}
          p={0}
          radius="md"
          className="customApi-wrapper board-grid-item-content harness-card"
          styles={{ root: { "--opacity": cfg.opacity, containerType: "size", overflowX: "hidden", overflowY: "auto" } as never }}
          data-grid-item-content
        >
          <CustomJsxRenderer
            template={cfg.template}
            data={cfg.data}
            status={cfg.status}
            options={cfg.options}
            components={components}
            createBindings={createCustomJsxBindings}
            messages={{
              noTemplate: "No JSX template is configured.",
              templateWarnings: (count: number) => `${count} template warnings:`,
              bindingTypeConflict: (name: string, a: string, b: string) => `Input ${name} conflicts between ${a} and ${b}`,
            }}
          />
        </Card>
      </CustomWidgetRuntimeProvider>
    </QueryClientProvider>
  </MantineProvider>,
);
