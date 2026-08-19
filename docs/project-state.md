<!-- GENERATED FILE - DO NOT EDIT.
     Regenerate with: python3 scripts/board.py sync
     The board is the source of truth; this file is a mirror. -->

# AI SDLC Platform — project state

**Generated:** 2026-08-19T13:25:26Z
**Source:** [https://linear.app/krukov-idea-hub/project/ai-sdlc-platform-ba96723ef010](https://linear.app/krukov-idea-hub/project/ai-sdlc-platform-ba96723ef010)
**Project status:** Planned
**Issues:** 55 live (4 in progress, 38 done) · 15 archived

## Project documents

- [00 · HUB — read this before any work](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927)
- [Конституция и видение проекта](https://linear.app/krukov-idea-hub/document/konstituciya-i-videnie-proekta-7f92af685fc1)
- [Референсная архитектура](https://linear.app/krukov-idea-hub/document/referensnaya-arhitektura-951bc7c33b59)

## Milestones

### 2. Research Skill for PO

Создать локальный skill, который исследует идею, координирует независимую проверку LLM, формирует утверждённые артефакты Feature и публикует их в Linear.

Корневая фича этапа — [IDE-80](https://linear.app/krukov-idea-hub/issue/IDE-80/feature-feature-discovery-skill). Читая её, видно состояние всего milestone.

**Здесь же лежат два хвоста milestone 1**, и это сделано осознанно:

* [IDE-101](https://linear.app/krukov-idea-hub/issue/IDE-101/work-item-ide-92-skill-installation-for-claude-codex-and-copilot) — установка скилла для Claude, Codex и Copilot. Формально ребёнок [IDE-92](https://linear.app/krukov-idea-hub/issue/IDE-92/feature-skvoznye-sluzhby-platformy) из milestone 1, но устанавливать нечего, пока не готов первый скилл, поэтому задача живёт здесь. Родитель в заголовке указан верно — он не переназначался.
* [IDE-87](https://linear.app/krukov-idea-hub/issue/IDE-87/work-item-ide-80-azure-devops-publishing-adapter) — адаптер Azure DevOps. Задача переформулирована: написать `scripts/sync_azure_devops_state.py` по интерфейсу адаптера Linear, а не отдельный скрипт публикации. Туда же разобран долг `read_token`, который читает `LINEAR_API_KEY` независимо от доски в профиле.

**Milestone 1 закрыт, гейт снят** — реализация здесь больше ничем не блокируется.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-101](https://linear.app/krukov-idea-hub/issue/IDE-101/work-item-ide-92-skill-installation-for-claude-codex-and-copilot) | [Work Item · IDE-92] Skill installation for Claude, Codex and Copilot | Backlog | — | `krukovden/ide-101-work-item-ide-92-skill-installation-for-claude-codex-and` | child of IDE-92, related IDE-99 |
| [IDE-112](https://linear.app/krukov-idea-hub/issue/IDE-112/work-item-ide-80-spike-as-an-artifact-type-in-schema-lint-and) | [Work Item · IDE-80] Spike as an artifact type in schema, lint and validator | Backlog | — | `krukovden/ide-112-work-item-ide-80-spike-as-an-artifact-type-in-schema-lint` | child of IDE-80, related IDE-102, related IDE-78 |
| [IDE-113](https://linear.app/krukov-idea-hub/issue/IDE-113/work-item-ide-92-three-consistency-debts-left-by-the-parallel-build) | [Work Item · IDE-92] Three consistency debts left by the parallel build | Backlog | — | `krukovden/ide-113-work-item-ide-92-three-consistency-debts-left-by-the` | child of IDE-92, related IDE-69, related IDE-105 |
| [IDE-106](https://linear.app/krukov-idea-hub/issue/IDE-106/work-item-ide-92-feature-history-order-and-entry-form-diverge-from-the) | [Work Item · IDE-92] Feature history order and entry form diverge from the contract | Todo | — | `krukovden/ide-106-work-item-ide-92-feature-history-order-and-entry-form` | child of IDE-92, related IDE-76, related IDE-105 |
| [IDE-88](https://linear.app/krukov-idea-hub/issue/IDE-88/work-item-ide-80-llm-evaluation-harness) | [Work Item · IDE-80] LLM evaluation harness | Todo | — | `krukovden/ide-88-work-item-ide-80-llm-evaluation-harness` | child of IDE-80 |
| [IDE-80](https://linear.app/krukov-idea-hub/issue/IDE-80/feature-feature-discovery-skill) | [Feature] Feature Discovery Skill | In Progress | Feature | `krukovden/ide-80-feature-feature-discovery-skill` | related IDE-114, related IDE-101, related IDE-98, related IDE-78, related IDE-92, related IDE-71 |
| [IDE-102](https://linear.app/krukov-idea-hub/issue/IDE-102/work-item-ide-80-content-validator-for-the-artifact-standard) | [Work Item · IDE-80] Content validator for the artifact standard | Done | — | `krukovden/ide-102-work-item-ide-80-content-validator-for-the-artifact-standard` | child of IDE-80, related IDE-78, related IDE-83 |
| [IDE-103](https://linear.app/krukov-idea-hub/issue/IDE-103/work-item-ide-80-reviewer-schema-and-fallback-under-strict-structured) | [Work Item · IDE-80] Reviewer schema and fallback under strict structured output | Done | — | `krukovden/ide-103-work-item-ide-80-reviewer-schema-and-fallback-under-strict` | child of IDE-80, related IDE-85, related IDE-68 |
| [IDE-104](https://linear.app/krukov-idea-hub/issue/IDE-104/work-item-ide-92-drift-detector-a-container-feature-is-satisfied-by) | [Work Item · IDE-92] Drift detector: a container feature is satisfied by its children | Done | — | `krukovden/ide-104-work-item-ide-92-drift-detector-a-container-feature-is` | child of IDE-92, related IDE-99, related IDE-94, related IDE-76, related IDE-93, related IDE-100, related IDE-95, related IDE-79, related IDE-101 |
| [IDE-105](https://linear.app/krukov-idea-hub/issue/IDE-105/work-item-ide-92-unified-naming-for-memory-files-at-both-levels) | [Work Item · IDE-92] Unified naming for memory files at both levels | Done | — | `krukovden/ide-105-work-item-ide-92-unified-naming-for-memory-files-at-both` | child of IDE-92, related IDE-79, related IDE-76, related IDE-80 |
| [IDE-111](https://linear.app/krukov-idea-hub/issue/IDE-111/work-item-ide-80-wire-the-content-validator-into-publication) | [Work Item · IDE-80] Wire the content validator into publication | Done | — | `krukovden/ide-111-work-item-ide-80-wire-the-content-validator-into-publication` | child of IDE-80, related IDE-102 |
| [IDE-68](https://linear.app/krukov-idea-hub/issue/IDE-68/spike-ide-80-design-the-deterministic-feature-discovery-skill) | [Spike · IDE-80] Design the deterministic Feature Discovery Skill | Done | Spike | `krukovden/ide-68-spike-ide-80-design-the-deterministic-feature-discovery` | child of IDE-80, blocks IDE-69 |
| [IDE-83](https://linear.app/krukov-idea-hub/issue/IDE-83/work-item-ide-80-repository-structure-for-skills-and-schemas) | [Work Item · IDE-80] Repository structure for skills and schemas | Done | — | `krukovden/ide-83-work-item-ide-80-repository-structure-for-skills-and-schemas` | child of IDE-80, blocks IDE-84 |
| [IDE-84](https://linear.app/krukov-idea-hub/issue/IDE-84/work-item-ide-80-discovery-core-state-machine-slot-registry-cli) | [Work Item · IDE-80] Discovery core: state machine, slot registry, CLI | Done | — | `krukovden/ide-84-work-item-ide-80-discovery-core-state-machine-slot-registry` | child of IDE-80, blocks IDE-88, blocks IDE-87, blocks IDE-86, blocks IDE-85 |
| [IDE-85](https://linear.app/krukov-idea-hub/issue/IDE-85/work-item-ide-80-reviewer-integration-and-practice-research) | [Work Item · IDE-80] Reviewer integration and practice research | Done | — | `krukovden/ide-85-work-item-ide-80-reviewer-integration-and-practice-research` | child of IDE-80 |
| [IDE-86](https://linear.app/krukov-idea-hub/issue/IDE-86/work-item-ide-80-linear-publishing-adapter) | [Work Item · IDE-80] Linear publishing adapter | Done | — | `krukovden/ide-86-work-item-ide-80-linear-publishing-adapter` | child of IDE-80, related IDE-93 |
| [IDE-87](https://linear.app/krukov-idea-hub/issue/IDE-87/work-item-ide-80-azure-devops-publishing-adapter) | [Work Item · IDE-80] Azure DevOps publishing adapter | Done | — | `krukovden/ide-87-work-item-ide-80-azure-devops-publishing-adapter` | child of IDE-80, related IDE-76, related IDE-93 |
| [IDE-10](https://linear.app/krukov-idea-hub/issue/IDE-10/add-grilling-research-and-independent-llm-review) | Add grilling, research, and independent LLM review | Canceled · archived | — | `krukovden/ide-10-add-grilling-research-and-independent-llm-review` | — |
| [IDE-11](https://linear.app/krukov-idea-hub/issue/IDE-11/generate-and-publish-the-approved-feature-package) | Generate and publish the approved Feature package | Canceled · archived | — | `krukovden/ide-11-generate-and-publish-the-approved-feature-package` | — |
| [IDE-9](https://linear.app/krukov-idea-hub/issue/IDE-9/build-the-local-feature-discovery-skill-entry-point) | Build the local Feature Discovery skill entry point | Canceled · archived | — | `krukovden/ide-9-build-the-local-feature-discovery-skill-entry-point` | — |

### 1. Фундамент и контракты

Определить правила игры проекта целиком: как платформа работает, куда смотрит, какими контрактами пользуется и как движется работа между этапами.

Результат этапа — не набор разрозненных решений, а **обновлённая документация на уровне проекта**: конституция, референсная архитектура и HUB, приведённые в соответствие с принятыми контрактами.

**Milestone закрыт 18 августа 2026.** Обе фичи завершены: [IDE-79](https://linear.app/krukov-idea-hub/issue/IDE-79/feature-proektirovanie-platformy-i-repozitoriya) — правила игры, шесть утверждённых Spike и сведение контрактов в документацию; [IDE-92](https://linear.app/krukov-idea-hub/issue/IDE-92/feature-skvoznye-sluzhby-platformy) — шесть сквозных служб на `main`, покрытых 276 офлайновыми тестами.

**Гейт снят.** Пока правила не были определены и сведены в документацию, этот milestone блокировал все остальные — иначе каждая возможность изобрела бы правила заново. Условие выполнено вместе с [IDE-89](https://linear.app/krukov-idea-hub/issue/IDE-89/work-item-ide-79-consolidate-the-contracts-into-project-documentation), приёмка прошла прогоном чистой сессии по одной документации. С этого момента реализация в следующих milestone не блокируется, и формулировку выше следует читать как историю, а не как действующий запрет.

**Что ушло в milestone 2:** адаптер Azure DevOps ([IDE-87](https://linear.app/krukov-idea-hub/issue/IDE-87/work-item-ide-80-azure-devops-publishing-adapter)) и установка скилла в Codex и Copilot ([IDE-101](https://linear.app/krukov-idea-hub/issue/IDE-101/work-item-ide-92-skill-installation-for-claude-codex-and-copilot)) — обе задачи ждут первого скилла, а не правил.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-100](https://linear.app/krukov-idea-hub/issue/IDE-100/work-item-ide-92-a-key-per-agent-identity-in-the-profile) | [Work Item · IDE-92] A key per agent: identity in the profile | Done | — | `krukovden/ide-100-work-item-ide-92-a-key-per-agent-identity-in-the-profile` | child of IDE-92, related IDE-71 |
| [IDE-125](https://linear.app/krukov-idea-hub/issue/IDE-125/work-item-ide-92-phase-positions-carried-by-tags-where-a-board-has-no) | [Work Item · IDE-92] Phase positions carried by tags where a board has no status | Done | — | `krukovden/ide-125-work-item-ide-92-phase-positions-carried-by-tags-where-a` | child of IDE-92, related IDE-87, related IDE-71 |
| [IDE-5](https://linear.app/krukov-idea-hub/issue/IDE-5/work-item-ide-79-establish-the-linked-linear-and-github-project) | [Work Item · IDE-79] Establish the linked Linear and GitHub project foundation | Done | — | `krukovden/ide-5-work-item-ide-79-establish-the-linked-linear-and-github` | child of IDE-79, related IDE-76 |
| [IDE-71](https://linear.app/krukov-idea-hub/issue/IDE-71/spike-ide-79-define-linear-workflow-states-approvals-and-agent) | [Spike · IDE-79] Define Linear workflow states, approvals, and agent handoffs | Done | Spike | `krukovden/ide-71-spike-ide-79-define-linear-workflow-states-approvals-and` | child of IDE-79, related IDE-77, blocks IDE-89, blocks IDE-72, blocks IDE-69, related IDE-68 |
| [IDE-76](https://linear.app/krukov-idea-hub/issue/IDE-76/spike-ide-79-design-the-project-memory-contract) | [Spike · IDE-79] Design the Project Memory contract | Done | Spike | `krukovden/ide-76-spike-ide-79-design-the-project-memory-contract` | child of IDE-79, blocks IDE-89, related IDE-68, related IDE-71 |
| [IDE-77](https://linear.app/krukov-idea-hub/issue/IDE-77/spike-ide-79-survey-agent-prior-art-and-make-reuse-review-a-standing) | [Spike · IDE-79] Survey agent prior art and make reuse review a standing rule | Done | Spike | `krukovden/ide-77-spike-ide-79-survey-agent-prior-art-and-make-reuse-review-a` | child of IDE-79, blocks IDE-89, blocks IDE-78, related IDE-76, related IDE-68 |
| [IDE-78](https://linear.app/krukov-idea-hub/issue/IDE-78/spike-ide-79-define-the-artifact-and-issue-authoring-standard) | [Spike · IDE-79] Define the artifact and issue authoring standard | Done | Spike | `krukovden/ide-78-spike-ide-79-define-the-artifact-and-issue-authoring` | child of IDE-79, related IDE-83, blocks IDE-89, related IDE-71, related IDE-76, related IDE-68 |
| [IDE-79](https://linear.app/krukov-idea-hub/issue/IDE-79/feature-proektirovanie-platformy-i-repozitoriya) | [Feature] Проектирование платформы и репозитория | Done | Feature, Process | `krukovden/ide-79-feature-proektirovanie-platformi-i-repozitoriya` | related IDE-87, related IDE-101, related IDE-99, related IDE-100, related IDE-93, related IDE-95, related IDE-94, related IDE-68, related IDE-77, blocks IDE-82 |
| [IDE-89](https://linear.app/krukov-idea-hub/issue/IDE-89/work-item-ide-79-consolidate-the-contracts-into-project-documentation) | [Work Item · IDE-79] Consolidate the contracts into project documentation | Done | — | `krukovden/ide-89-work-item-ide-79-consolidate-the-contracts-into-project` | child of IDE-79, related IDE-94, related IDE-100 |
| [IDE-90](https://linear.app/krukov-idea-hub/issue/IDE-90/spike-ide-79-define-platform-elements-phases-and-how-each-phase-is) | [Spike · IDE-79] Define platform elements, phases and how each phase is triggered | Done | Spike | `krukovden/ide-90-spike-ide-79-define-platform-elements-phases-and-how-each` | child of IDE-79, related IDE-78, related IDE-77, blocks IDE-89, blocks IDE-71, related IDE-76 |
| [IDE-92](https://linear.app/krukov-idea-hub/issue/IDE-92/feature-skvoznye-sluzhby-platformy) | [Feature] Сквозные службы платформы | Done | Feature | `krukovden/ide-92-feature-skvoznie-sluzhbi-platformi` | related IDE-103, related IDE-87, related IDE-79, related IDE-76 |
| [IDE-93](https://linear.app/krukov-idea-hub/issue/IDE-93/work-item-ide-92-tracker-adapter-and-profile-resolution) | [Work Item · IDE-92] Tracker adapter and profile resolution | Done | — | `krukovden/ide-93-work-item-ide-92-tracker-adapter-and-profile-resolution` | child of IDE-92, related IDE-76 |
| [IDE-94](https://linear.app/krukov-idea-hub/issue/IDE-94/work-item-ide-92-state-resolver-and-idp-status) | [Work Item · IDE-92] State resolver and /idp-status | Done | — | `krukovden/ide-94-work-item-ide-92-state-resolver-and-idp-status` | child of IDE-92, related IDE-90 |
| [IDE-95](https://linear.app/krukov-idea-hub/issue/IDE-95/work-item-ide-92-project-memory-implementation) | [Work Item · IDE-92] Project memory implementation | Done | — | `krukovden/ide-95-work-item-ide-92-project-memory-implementation` | child of IDE-92, related IDE-76 |
| [IDE-99](https://linear.app/krukov-idea-hub/issue/IDE-99/work-item-ide-92-platform-installation-and-per-project-onboarding) | [Work Item · IDE-92] Platform installation and per-project onboarding | Done | — | `krukovden/ide-99-work-item-ide-92-platform-installation-and-per-project` | child of IDE-92, related IDE-100, related IDE-90, related IDE-77, related IDE-71 |
| [IDE-6](https://linear.app/krukov-idea-hub/issue/IDE-6/define-the-platform-domain-model-and-terminology) | Define the platform domain model and terminology | Canceled · archived | — | `krukovden/ide-6-define-the-platform-domain-model-and-terminology` | — |
| [IDE-7](https://linear.app/krukov-idea-hub/issue/IDE-7/define-versioned-artifact-contracts-and-validation) | Define versioned artifact contracts and validation | Canceled · archived | — | `krukovden/ide-7-define-versioned-artifact-contracts-and-validation` | — |
| [IDE-8](https://linear.app/krukov-idea-hub/issue/IDE-8/define-the-project-profile-configuration-contract) | Define the Project Profile configuration contract | Canceled · archived | — | `krukovden/ide-8-define-the-project-profile-configuration-contract` | — |
| [IDE-91](https://linear.app/krukov-idea-hub/issue/IDE-91/feature-way-of-working) | [Feature] Way of working | Canceled | Feature | `krukovden/ide-91-feature-way-of-working` | related IDE-78, related IDE-77, related IDE-79 |

### 5. Пилот

Прогнать весь процесс целиком на пилотном проекте Private AI Knowledge Platform MVP: от идеи Product Owner до смерженного PR и обновлённой документации.

Это не стройка, а проверка. Проект закончен, когда процесс отработал на пилоте.

**Синхронизация документации переехала в milestone 4.** Documenter вызывается ведущим скриптом внутри цепочки PBI, сразу после лида — отдельного этапа под одного участника не нужно.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-114](https://linear.app/krukov-idea-hub/issue/IDE-114/work-item-pilot-live-verification-of-the-chain-on-azure-devops) | [Work Item · Pilot] Live verification of the chain on Azure DevOps | Backlog | — | `krukovden/ide-114-work-item-pilot-live-verification-of-the-chain-on-azure` | blocks IDE-88, related IDE-103, related IDE-87, related IDE-108, related IDE-107 |
| [IDE-98](https://linear.app/krukov-idea-hub/issue/IDE-98/work-item-pilot-run-the-full-process-on-private-ai-knowledge-platform) | [Work Item · Pilot] Run the full process on Private AI Knowledge Platform MVP | Backlog | — | `krukovden/ide-98-work-item-pilot-run-the-full-process-on-private-ai-knowledge` | — |
| [IDE-18](https://linear.app/krukov-idea-hub/issue/IDE-18/build-documentation-impact-analysis-and-update-workflow) | Build documentation impact analysis and update workflow | Canceled · archived | — | `krukovden/ide-18-build-documentation-impact-analysis-and-update-workflow` | — |
| [IDE-19](https://linear.app/krukov-idea-hub/issue/IDE-19/run-the-end-to-end-pilot-on-private-ai-knowledge-platform-mvp) | Run the end-to-end pilot on Private AI Knowledge Platform MVP | Canceled · archived | — | `krukovden/ide-19-run-the-end-to-end-pilot-on-private-ai-knowledge-platform` | — |
| [IDE-20](https://linear.app/krukov-idea-hub/issue/IDE-20/evaluate-the-pilot-and-complete-ai-sdlc-platform-v1) | Evaluate the pilot and complete AI SDLC Platform v1 | Canceled · archived | — | `krukovden/ide-20-evaluate-the-pilot-and-complete-ai-sdlc-platform-v1` | — |

### 4. Development

Поток разработки: от передачи утверждённого плана в реализацию до доказательства, что сделанное соответствует утверждённому.

Здесь решаются все подпункты цикла разработки:

* **Ручная передача в реализацию** — разработчик запускает работу явной командой, локально или в облаке, любым совместимым агентом.
* **Интеграция с репозиторием** — ветки, коммиты и pull requests, связанные с задачей идентификатором.
* **Review артефакт** — проверка не качества кода, а верности модели: соответствует ли реализация утверждённой Feature, дизайну и доменным договорённостям. Результат бинарный: Pass или Block.
* **QA Evidence** — матрица, где каждый критерий приёмки получает статус PASS, FAIL или BLOCKED вместе с доказательством. Не «тесты прошли», а «вот критерий, вот чем он подтверждён».
* **Completion Metadata** — итог ревью, итог QA и дифф документации, записанные в одном месте вместе с задачей, а не разбросанные по системе.
* **Follow-up Ledger** — журнал хвостов: что нашли по дороге и решили не делать сейчас. Хвост остаётся привязанным к задаче, которая его породила, вместе с её контекстом, и не улетает в общий бэклог, где через месяц никто не вспомнит, откуда он взялся.

Последние четыре пункта заимствованы из Artifact-Driven-Development, где они лежат в одном документе на задачу — приём, который там называется trace co-location: связанные артефакты держатся вместе, потому что так трасса читается.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-96](https://linear.app/krukov-idea-hub/issue/IDE-96/feature-komanda-idp-development-cepochka-realizacii) | [Feature] Команда /idp-development — цепочка реализации | Backlog | Feature | `krukovden/ide-96-feature-komanda-idp-development-cepochka-realizacii` | related IDE-82 |
| [IDE-97](https://linear.app/krukov-idea-hub/issue/IDE-97/spike-ide-96-design-the-pbi-chain-contract) | [Spike · IDE-96] Design the PBI chain contract | Backlog | Spike | `krukovden/ide-97-spike-ide-96-design-the-pbi-chain-contract` | child of IDE-96, related IDE-71, related IDE-90, related IDE-72 |
| [IDE-15](https://linear.app/krukov-idea-hub/issue/IDE-15/build-the-manual-implementation-handoff) | Build the manual implementation handoff | Canceled · archived | — | `krukovden/ide-15-build-the-manual-implementation-handoff` | — |
| [IDE-16](https://linear.app/krukov-idea-hub/issue/IDE-16/integrate-github-branches-commits-and-pull-requests) | Integrate GitHub branches, commits, and pull requests | Canceled · archived | — | `krukovden/ide-16-integrate-github-branches-commits-and-pull-requests` | — |
| [IDE-17](https://linear.app/krukov-idea-hub/issue/IDE-17/capture-implementation-verification-and-pr-summaries) | Capture implementation verification and PR summaries | Canceled · archived | — | `krukovden/ide-17-capture-implementation-verification-and-pr-summaries` | — |

### 3. Технический дизайн и планирование

Превратить фичу в ADR и утверждённый ADR — в PBI.

`/idp-design` подхватывает созданную Product Owner фичу и пишет ADR: архитектура, затронутые компоненты, контракты, риски, чем платим. Человек утверждает его на доске (статус Design Review) — это второй гейт; утверждённый ADR прикрепляется файлом к фиче.

`/idp-planning` разворачивает утверждённый ADR в PBI с критериями приёмки и зависимостями и создаёт ветку фичи.

Слово *Spike* здесь больше не означает технический дизайн — технический дизайн это ADR.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-123](https://linear.app/krukov-idea-hub/issue/IDE-123/work-item-ide-81-idp-design-bug-route-verdict) | [Work Item · IDE-81] /idp-design: bug-route verdict | Backlog | — | `krukovden/ide-123-work-item-ide-81-idp-design-bug-route-verdict` | child of IDE-81, related IDE-107, related IDE-69 |
| [IDE-124](https://linear.app/krukov-idea-hub/issue/IDE-124/work-item-ide-81-idp-design-reissue-after-escalation-superseding-the) | [Work Item · IDE-81] /idp-design: reissue after escalation, superseding the previous ADR | Backlog | — | `krukovden/ide-124-work-item-ide-81-idp-design-reissue-after-escalation` | child of IDE-81, related IDE-76, related IDE-69 |
| [IDE-81](https://linear.app/krukov-idea-hub/issue/IDE-81/feature-komanda-idp-design-tehnicheskij-dizajn) | [Feature] Команда /idp-design — технический дизайн | In Progress | Feature | `krukovden/ide-81-feature-komanda-idp-design-tekhnicheskii-dizain` | related IDE-82, related IDE-90 |
| [IDE-82](https://linear.app/krukov-idea-hub/issue/IDE-82/feature-komanda-idp-planning-dekompoziciya-na-pbi) | [Feature] Команда /idp-planning — декомпозиция на PBI | In Progress | Feature | `krukovden/ide-82-feature-komanda-idp-planning-dekompoziciya-na-pbi` | related IDE-90, related IDE-78 |
| [IDE-107](https://linear.app/krukov-idea-hub/issue/IDE-107/work-item-ide-81-idp-design-core-subphases-decision-registry) | [Work Item · IDE-81] /idp-design core: subphases, decision registry, alternatives budget | Done | — | `krukovden/ide-107-work-item-ide-81-idp-design-core-subphases-decision-registry` | child of IDE-81, related IDE-69, related IDE-103 |
| [IDE-108](https://linear.app/krukov-idea-hub/issue/IDE-108/work-item-ide-82-idp-planning-core-atomicity-path-overlap-graph) | [Work Item · IDE-82] /idp-planning core: atomicity, path overlap graph, feature branch | Done | — | `krukovden/ide-108-work-item-ide-82-idp-planning-core-atomicity-path-overlap` | child of IDE-82, related IDE-72 |
| [IDE-69](https://linear.app/krukov-idea-hub/issue/IDE-69/spike-ide-81-design-the-idp-design-command) | [Spike · IDE-81] Design the /idp-design command | Done | Spike | `krukovden/ide-69-spike-ide-81-design-the-idp-design-command` | child of IDE-81, related IDE-90, related IDE-78, blocks IDE-72 |
| [IDE-72](https://linear.app/krukov-idea-hub/issue/IDE-72/spike-ide-82-design-the-idp-planning-command) | [Spike · IDE-82] Design the /idp-planning command | Done | Spike | `krukovden/ide-72-spike-ide-82-design-the-idp-planning-command` | child of IDE-82, related IDE-78, related IDE-90 |
| [IDE-12](https://linear.app/krukov-idea-hub/issue/IDE-12/implement-the-tech-lead-design-approval-gate) | Implement the Tech Lead design approval gate | Canceled · archived | — | `krukovden/ide-12-implement-the-tech-lead-design-approval-gate` | — |
| [IDE-13](https://linear.app/krukov-idea-hub/issue/IDE-13/generate-implementation-tasks-from-the-approved-spike) | Generate implementation tasks from the approved Spike | Canceled · archived | — | `krukovden/ide-13-generate-implementation-tasks-from-the-approved-spike` | — |
| [IDE-14](https://linear.app/krukov-idea-hub/issue/IDE-14/generate-the-spike-technical-design-from-an-approved-feature) | Generate the Spike technical design from an approved Feature | Canceled · archived | — | `krukovden/ide-14-generate-the-spike-technical-design-from-an-approved-feature` | — |

### 6. Establish project

Start a whole project, not a single feature.

The Product Owner arrives with an architecture already thought through and wants it verified, sliced into features, and put on the board — then delivered stage by stage. The existing pipeline has no entry at this level: it always assumes a live repository, a live board and one idea at a time.

This milestone builds `/idp-establish`: it verifies the supplied architecture can actually carry the product, turns it into a project-scope ADR, slices it into stages and features, and blocks every feature that is not thought through until Feature Discovery has run on it.

It precedes the pilot: the pilot verifies the process, this milestone gives the process a way to begin.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-109](https://linear.app/krukov-idea-hub/issue/IDE-109/feature-establish-project) | [Feature] Establish project | Backlog | Feature | `krukovden/ide-109-feature-establish-project` | — |
| [IDE-122](https://linear.app/krukov-idea-hub/issue/IDE-122/work-item-ide-109-documentation-constitution-reference-architecture) | [Work Item · IDE-109] Documentation: constitution, reference architecture, HUB, CLAUDE.md | In Development | — | `krukovden/ide-122-work-item-ide-109-documentation-constitution-reference` | child of IDE-109, related IDE-110 |
| [IDE-110](https://linear.app/krukov-idea-hub/issue/IDE-110/spike-ide-109-design-the-establish-project-phase) | [Spike · IDE-109] Design the establish-project phase | Done | Spike | `krukovden/ide-110-spike-ide-109-design-the-establish-project-phase` | child of IDE-109, related IDE-93, related IDE-87 |
| [IDE-115](https://linear.app/krukov-idea-hub/issue/IDE-115/work-item-ide-109-contract-changes-frontmatter-project-adr-template) | [Work Item · IDE-109] Contract changes: frontmatter, project ADR template, lint, state resolver | Done | — | `krukovden/ide-115-work-item-ide-109-contract-changes-frontmatter-project-adr` | child of IDE-109, blocks IDE-117, related IDE-110 |
| [IDE-116](https://linear.app/krukov-idea-hub/issue/IDE-116/work-item-ide-109-adapter-work-item-kinds-and-creation-by-kind) | [Work Item · IDE-109] Adapter: work-item kinds and creation by kind | Done | — | `krukovden/ide-116-work-item-ide-109-adapter-work-item-kinds-and-creation-by` | child of IDE-109, blocks IDE-120, related IDE-110 |
| [IDE-117](https://linear.app/krukov-idea-hub/issue/IDE-117/work-item-ide-109-establish-core-state-machine-project-slot-registry) | [Work Item · IDE-109] Establish core: state machine, project slot registry, intake and coverage | Done | — | `krukovden/ide-117-work-item-ide-109-establish-core-state-machine-project-slot` | child of IDE-109, blocks IDE-118, related IDE-110 |
| [IDE-118](https://linear.app/krukov-idea-hub/issue/IDE-118/work-item-ide-109-challenge-and-traversal-the-falsifiable-verdict) | [Work Item · IDE-109] Challenge and traversal: the falsifiable verdict | Done | — | `krukovden/ide-118-work-item-ide-109-challenge-and-traversal-the-falsifiable` | child of IDE-109, blocks IDE-119, related IDE-110 |
| [IDE-119](https://linear.app/krukov-idea-hub/issue/IDE-119/work-item-ide-109-slicing-the-escalation-rule-and-the-per-feature) | [Work Item · IDE-109] Slicing, the escalation rule and the per-feature review | Done | — | `krukovden/ide-119-work-item-ide-109-slicing-the-escalation-rule-and-the-per` | child of IDE-109, blocks IDE-120, related IDE-110 |
| [IDE-120](https://linear.app/krukov-idea-hub/issue/IDE-120/work-item-ide-109-approval-hashing-and-idempotent-publication) | [Work Item · IDE-109] Approval, hashing and idempotent publication | Done | — | `krukovden/ide-120-work-item-ide-109-approval-hashing-and-idempotent` | child of IDE-109, blocks IDE-121, related IDE-110 |
| [IDE-121](https://linear.app/krukov-idea-hub/issue/IDE-121/work-item-ide-109-wiki-writer-architecture-and-flow-pages-optional) | [Work Item · IDE-109] Wiki writer: architecture and flow pages, optional path | Done | — | `krukovden/ide-121-work-item-ide-109-wiki-writer-architecture-and-flow-pages` | child of IDE-109, related IDE-110 |

## How to use this file

This is a snapshot. For anything that must be current — a status right now, the full text of an issue, comments, or an approval record — ask the board directly: `board.py show`, `board.py list`. Use this file for orientation, for offline work, and to see in `git log` how the shape of the work changed over time.
