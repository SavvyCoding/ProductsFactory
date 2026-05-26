"""Static catalogues of tech stacks and UI/UX templates offered in the
New Product wizard.

Single source of truth — the wizard renders from these, the recommendation
endpoints validate against them, and the coder agent receives the chosen
ID and uses it to scaffold matching code on first session.

Adding entries here is enough to make them appear in the wizard and be
accepted by the recommendation endpoints. Templates are generated lazily
by the agent on first session (no per-stack starter dir required).
"""

from typing import TypedDict


class StackOption(TypedDict):
    id:          str   # canonical id, snake_case
    label:       str   # human label for the wizard
    has_web_ui:  bool  # True → Step 4 (UI template) shown
    description: str   # one-liner used in card UI and AI recommendation prompt


class StackGroup(TypedDict):
    label:   str
    options: list[StackOption]


# --- Tech stacks ─────────────────────────────────────────────────────────────
# Grouped for the wizard UI; ids are flat (used by API + agent).

STACK_CATALOG: list[StackGroup] = [
    {
        "label": "API backend",
        "options": [
            {"id": "python_fastapi",   "label": "Python + FastAPI",   "has_web_ui": False, "description": "Modern async Python web API. Strong typing, OpenAPI built in."},
            {"id": "python_flask",     "label": "Python + Flask",     "has_web_ui": False, "description": "Minimalist Python web framework. Great for small services."},
            {"id": "python_django",    "label": "Python + Django",    "has_web_ui": True,  "description": "Batteries-included Python framework. Admin, ORM, auth out of the box."},
            {"id": "node_express",     "label": "Node.js + Express",  "has_web_ui": False, "description": "Most popular Node web framework. Mature ecosystem."},
            {"id": "node_nestjs",      "label": "Node.js + NestJS",   "has_web_ui": False, "description": "Opinionated TypeScript framework. Angular-style modules + DI."},
            {"id": "node_fastify",     "label": "Node.js + Fastify",  "has_web_ui": False, "description": "Fast lightweight Node API. Schema-first, JSON-Schema validation."},
            {"id": "go_chi",           "label": "Go + Chi",           "has_web_ui": False, "description": "Idiomatic Go HTTP router. Tiny, composable."},
            {"id": "go_gin",           "label": "Go + Gin",           "has_web_ui": False, "description": "Most popular Go web framework. Fast, batteries-included."},
            {"id": "rust_axum",        "label": "Rust + Axum",        "has_web_ui": False, "description": "Tokio-based async Rust framework. Type-safe routing."},
            {"id": "java_spring_boot", "label": "Java + Spring Boot", "has_web_ui": True,  "description": "Enterprise Java framework. Massive ecosystem, mature tooling."},
        ],
    },
    {
        "label": "Full-stack web",
        "options": [
            {"id": "nextjs",     "label": "Next.js",     "has_web_ui": True, "description": "React framework with SSR. Most popular full-stack TypeScript choice."},
            {"id": "remix",      "label": "Remix",       "has_web_ui": True, "description": "React framework focused on web fundamentals (forms, progressive enhancement)."},
            {"id": "sveltekit",  "label": "SvelteKit",   "has_web_ui": True, "description": "Svelte's full-stack framework. Smallest bundles, simplest mental model."},
            {"id": "nuxt",       "label": "Nuxt (Vue)",  "has_web_ui": True, "description": "Vue's full-stack framework. SSR + file-based routing."},
            {"id": "astro",      "label": "Astro",       "has_web_ui": True, "description": "Content-focused web framework. Mix React/Vue/Svelte islands as needed."},
            {"id": "t3_stack",   "label": "T3 (Next + tRPC + Prisma)", "has_web_ui": True, "description": "Type-safe full stack. tRPC for end-to-end types, Prisma for DB."},
            {"id": "rails",      "label": "Ruby on Rails", "has_web_ui": True, "description": "Convention-over-config Ruby framework. Fast prototype to production."},
            {"id": "laravel",    "label": "PHP + Laravel", "has_web_ui": True, "description": "Modern PHP framework. Eloquent ORM, Blade templates, large ecosystem."},
        ],
    },
    {
        "label": "Mobile",
        "options": [
            {"id": "react_native_expo", "label": "React Native (Expo)",  "has_web_ui": False, "description": "JavaScript cross-platform mobile. Expo handles native build."},
            {"id": "flutter",           "label": "Flutter",              "has_web_ui": False, "description": "Dart cross-platform mobile + desktop. Single codebase, custom rendering."},
            {"id": "swiftui",           "label": "SwiftUI (iOS/macOS)",  "has_web_ui": False, "description": "Apple's native UI framework. iOS, iPadOS, macOS, watchOS."},
            {"id": "kotlin_compose",    "label": "Kotlin + Jetpack Compose", "has_web_ui": False, "description": "Android native UI in Kotlin. Modern declarative."},
        ],
    },
    {
        "label": "Data / ML",
        "options": [
            {"id": "python_streamlit", "label": "Python + Streamlit", "has_web_ui": True,  "description": "Quick data app builder. Best for internal tools and demos."},
            {"id": "python_gradio",    "label": "Python + Gradio",    "has_web_ui": True,  "description": "ML demo UI. Hugging Face's go-to for model interfaces."},
            {"id": "python_jupyter",   "label": "Python + Jupyter",   "has_web_ui": False, "description": "Notebook-driven analysis. For exploratory data work."},
        ],
    },
    {
        "label": "Static site",
        "options": [
            {"id": "astro_static",  "label": "Astro (static)",  "has_web_ui": True, "description": "Astro in static-output mode. Fast marketing/docs sites."},
            {"id": "hugo",          "label": "Hugo",            "has_web_ui": True, "description": "Go-based static site generator. Famously fast builds."},
            {"id": "mkdocs",        "label": "MkDocs",          "has_web_ui": True, "description": "Markdown-driven documentation sites. Simple and battle-tested."},
            {"id": "docusaurus",    "label": "Docusaurus",      "has_web_ui": True, "description": "React-based documentation framework. Used by Meta + many OSS projects."},
        ],
    },
]


# Flat lookup: id → option for validation + agent-side metadata
STACK_BY_ID: dict[str, StackOption] = {
    opt["id"]: opt
    for group in STACK_CATALOG
    for opt in group["options"]
}


# --- Database options (sub-pick once stack is chosen) ────────────────────────
DATABASE_OPTIONS = [
    {"id": "postgresql", "label": "PostgreSQL", "description": "Default choice. Strong type system, JSONB, full-text, great Python/Node support."},
    {"id": "mysql",      "label": "MySQL",      "description": "Widely deployed, good for vanilla CRUD. Pick if your team knows it."},
    {"id": "mongodb",    "label": "MongoDB",    "description": "Document store. Good when schema evolves rapidly."},
    {"id": "sqlite",     "label": "SQLite",     "description": "Embedded. Zero ops. Great for prototypes and single-user tools."},
    {"id": "redis",      "label": "Redis",      "description": "In-memory K/V store. Cache or queue, not a primary DB."},
    {"id": "none",       "label": "No database", "description": "Pure compute / static content. No persistence layer needed."},
]

DATABASE_BY_ID: dict[str, dict] = {opt["id"]: opt for opt in DATABASE_OPTIONS}


# --- UI/UX templates (Step 4, only if stack has_web_ui) ──────────────────────

class UITemplate(TypedDict):
    id:          str
    label:       str
    description: str
    style_hint:  str   # passed to coder agent so it picks matching libs/styles


UI_TEMPLATES: list[UITemplate] = [
    {
        "id": "modern_saas",
        "label": "Modern SaaS",
        "description": "Clean, contemporary look. Tailwind + shadcn/ui style. Linear / Vercel / Supabase aesthetic.",
        "style_hint": "Use Tailwind CSS with shadcn/ui-style components. Subdued palette (greys + one accent), generous whitespace, Inter or Geist font, subtle borders, dark mode by default.",
    },
    {
        "id": "material_3",
        "label": "Material 3 (Google)",
        "description": "Google's Material Design 3 system. Tonal palettes, dynamic colour, FABs and chips.",
        "style_hint": "Use Material UI (MUI) for React or Material Web Components. Follow MD3 tokens: tonal palettes, elevated/filled surfaces, FAB for primary action.",
    },
    {
        "id": "apple_hig",
        "label": "Apple HIG",
        "description": "Apple Human Interface Guidelines. Native iOS/macOS feel with rounded cards and SF Pro typography.",
        "style_hint": "Mimic Apple HIG: SF Pro / Inter, rounded 12px corners, soft shadows, frosted-glass effects, iOS-style tab bars or sidebars.",
    },
    {
        "id": "bootstrap",
        "label": "Bootstrap classic",
        "description": "Familiar enterprise look. Bootstrap 5 components — fast, recognisable, low-risk.",
        "style_hint": "Use Bootstrap 5 components and grid. Default theme; navbar + cards + tables + modals. Optimise for fast page builds, not custom design.",
    },
    {
        "id": "brutalist",
        "label": "Brutalist / Editorial",
        "description": "Heavy borders, monospace, asymmetric layouts. On-trend for indie tools.",
        "style_hint": "Heavy 2-3px solid black borders, monospace fonts (JetBrains Mono / IBM Plex Mono), high contrast, deliberately raw layouts. Inspirations: Vercel beta sites, indie hacker tools.",
    },
    {
        "id": "minimalist",
        "label": "Minimalist (Stripe / Notion)",
        "description": "Quiet, content-first design. Lots of whitespace, restrained colour, clear typography.",
        "style_hint": "Minimal palette (1 accent + greys). Lots of whitespace. Typography-led. Inter or Söhne fonts. Inspirations: Stripe.com, Notion, Linear marketing pages.",
    },
    {
        "id": "agent_choose",
        "label": "Let the agent choose",
        "description": "No preset — the coder reads your vision and picks something appropriate. Default.",
        "style_hint": "No specific style mandated. Pick a CSS framework and component library that fits the product vision.",
    },
]

UI_TEMPLATE_BY_ID: dict[str, UITemplate] = {tpl["id"]: tpl for tpl in UI_TEMPLATES}
