<!-- GENERATED FILE - DO NOT EDIT.
     Regenerate with: python3 scripts/sync_linear_state.py
     Linear is the source of truth; this file is a mirror. -->

# AI SDLC Platform — project state

**Generated:** 2026-08-14T18:41:15Z
**Source:** [https://linear.app/krukov-idea-hub/project/ai-sdlc-platform-ba96723ef010](https://linear.app/krukov-idea-hub/project/ai-sdlc-platform-ba96723ef010)
**Project status:** Planned
**Issues:** 19 live (2 in progress, 2 done) · 15 archived

## Project documents

- [00 · HUB — read this before any work](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927)
- [Конституция и видение проекта](https://linear.app/krukov-idea-hub/document/konstituciya-i-videnie-proekta-7f92af685fc1)
- [Референсная архитектура](https://linear.app/krukov-idea-hub/document/referensnaya-arhitektura-951bc7c33b59)

## Milestones

### 2. Research Skill for PO

Создать локальный skill, который исследует идею, координирует независимую проверку LLM, формирует утверждённые артефакты Feature и публикует их в Linear.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-80](https://linear.app/krukov-idea-hub/issue/IDE-80/feature-feature-discovery-skill) | [Feature] Feature Discovery Skill | Backlog | Feature | `krukovden/ide-80-feature-feature-discovery-skill` | related IDE-71 |
| [IDE-83](https://linear.app/krukov-idea-hub/issue/IDE-83/work-item-ide-80-repository-structure-for-skills-and-schemas) | [Work Item · IDE-80] Repository structure for skills and schemas | Todo | — | `krukovden/ide-83-work-item-ide-80-repository-structure-for-skills-and-schemas` | child of IDE-80, blocks IDE-84 |
| [IDE-84](https://linear.app/krukov-idea-hub/issue/IDE-84/work-item-ide-80-discovery-core-state-machine-slot-registry-cli) | [Work Item · IDE-80] Discovery core: state machine, slot registry, CLI | Todo | — | `krukovden/ide-84-work-item-ide-80-discovery-core-state-machine-slot-registry` | child of IDE-80, blocks IDE-88, blocks IDE-87, blocks IDE-86, blocks IDE-85 |
| [IDE-85](https://linear.app/krukov-idea-hub/issue/IDE-85/work-item-ide-80-reviewer-integration-and-practice-research) | [Work Item · IDE-80] Reviewer integration and practice research | Todo | — | `krukovden/ide-85-work-item-ide-80-reviewer-integration-and-practice-research` | child of IDE-80 |
| [IDE-86](https://linear.app/krukov-idea-hub/issue/IDE-86/work-item-ide-80-linear-publishing-adapter) | [Work Item · IDE-80] Linear publishing adapter | Todo | — | `krukovden/ide-86-work-item-ide-80-linear-publishing-adapter` | child of IDE-80 |
| [IDE-87](https://linear.app/krukov-idea-hub/issue/IDE-87/work-item-ide-80-azure-devops-publishing-adapter) | [Work Item · IDE-80] Azure DevOps publishing adapter | Todo | — | `krukovden/ide-87-work-item-ide-80-azure-devops-publishing-adapter` | child of IDE-80 |
| [IDE-88](https://linear.app/krukov-idea-hub/issue/IDE-88/work-item-ide-80-llm-evaluation-harness) | [Work Item · IDE-80] LLM evaluation harness | Todo | — | `krukovden/ide-88-work-item-ide-80-llm-evaluation-harness` | child of IDE-80 |
| [IDE-68](https://linear.app/krukov-idea-hub/issue/IDE-68/spike-ide-80-design-the-deterministic-feature-discovery-skill) | [Spike · IDE-80] Design the deterministic Feature Discovery Skill | Done | Spike | `krukovden/ide-68-spike-ide-80-design-the-deterministic-feature-discovery` | child of IDE-80, blocks IDE-69 |
| [IDE-10](https://linear.app/krukov-idea-hub/issue/IDE-10/add-grilling-research-and-independent-llm-review) | Add grilling, research, and independent LLM review | Canceled · archived | — | `krukovden/ide-10-add-grilling-research-and-independent-llm-review` | — |
| [IDE-11](https://linear.app/krukov-idea-hub/issue/IDE-11/generate-and-publish-the-approved-feature-package) | Generate and publish the approved Feature package | Canceled · archived | — | `krukovden/ide-11-generate-and-publish-the-approved-feature-package` | — |
| [IDE-9](https://linear.app/krukov-idea-hub/issue/IDE-9/build-the-local-feature-discovery-skill-entry-point) | Build the local Feature Discovery skill entry point | Canceled · archived | — | `krukovden/ide-9-build-the-local-feature-discovery-skill-entry-point` | — |

### 1. Фундамент и контракты

Определить правила игры проекта целиком: как платформа работает, куда смотрит, какими контрактами пользуется и как движется работа между этапами.

Результат этапа — не набор разрозненных решений, а **обновлённая документация на уровне проекта**: конституция, референсная архитектура и HUB, приведённые в соответствие с принятыми контрактами.

**Этот milestone блокирует все остальные.** Пока правила не определены и не сведены в документацию, реализация возможностей не начинается — иначе каждая из них изобретёт свои правила заново.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-71](https://linear.app/krukov-idea-hub/issue/IDE-71/spike-ide-79-define-linear-workflow-states-approvals-and-agent) | [Spike · IDE-79] Define Linear workflow states, approvals, and agent handoffs | Backlog | Spike | `krukovden/ide-71-spike-ide-79-define-linear-workflow-states-approvals-and` | child of IDE-79, blocks IDE-89, blocks IDE-72, blocks IDE-69, related IDE-68 |
| [IDE-76](https://linear.app/krukov-idea-hub/issue/IDE-76/spike-ide-79-design-the-project-memory-contract) | [Spike · IDE-79] Design the Project Memory contract | Backlog | Spike | `krukovden/ide-76-spike-ide-79-design-the-project-memory-contract` | child of IDE-79, blocks IDE-89, related IDE-68, related IDE-71 |
| [IDE-78](https://linear.app/krukov-idea-hub/issue/IDE-78/spike-ide-79-define-the-artifact-and-issue-authoring-standard) | [Spike · IDE-79] Define the artifact and issue authoring standard | Backlog | Spike | `krukovden/ide-78-spike-ide-79-define-the-artifact-and-issue-authoring` | child of IDE-79, blocks IDE-89, related IDE-71, related IDE-76, related IDE-68 |
| [IDE-89](https://linear.app/krukov-idea-hub/issue/IDE-89/work-item-ide-79-consolidate-the-contracts-into-project-documentation) | [Work Item · IDE-79] Consolidate the contracts into project documentation | Backlog | — | `krukovden/ide-89-work-item-ide-79-consolidate-the-contracts-into-project` | child of IDE-79 |
| [IDE-77](https://linear.app/krukov-idea-hub/issue/IDE-77/spike-ide-79-survey-agent-prior-art-and-make-reuse-review-a-standing) | [Spike · IDE-79] Survey agent prior art and make reuse review a standing rule | In Progress | Spike | `krukovden/ide-77-spike-ide-79-survey-agent-prior-art-and-make-reuse-review-a` | child of IDE-79, blocks IDE-89, blocks IDE-78, related IDE-76, related IDE-68 |
| [IDE-79](https://linear.app/krukov-idea-hub/issue/IDE-79/feature-platform-foundation-and-contracts) | [Feature] Platform foundation and contracts | In Progress | Feature | `krukovden/ide-79-feature-platform-foundation-and-contracts` | blocks IDE-82, blocks IDE-81, blocks IDE-80 |
| [IDE-5](https://linear.app/krukov-idea-hub/issue/IDE-5/work-item-ide-79-establish-the-linked-linear-and-github-project) | [Work Item · IDE-79] Establish the linked Linear and GitHub project foundation | Done | — | `krukovden/ide-5-work-item-ide-79-establish-the-linked-linear-and-github` | child of IDE-79, related IDE-76 |
| [IDE-6](https://linear.app/krukov-idea-hub/issue/IDE-6/define-the-platform-domain-model-and-terminology) | Define the platform domain model and terminology | Canceled · archived | — | `krukovden/ide-6-define-the-platform-domain-model-and-terminology` | — |
| [IDE-7](https://linear.app/krukov-idea-hub/issue/IDE-7/define-versioned-artifact-contracts-and-validation) | Define versioned artifact contracts and validation | Canceled · archived | — | `krukovden/ide-7-define-versioned-artifact-contracts-and-validation` | — |
| [IDE-8](https://linear.app/krukov-idea-hub/issue/IDE-8/define-the-project-profile-configuration-contract) | Define the Project Profile configuration contract | Canceled · archived | — | `krukovden/ide-8-define-the-project-profile-configuration-contract` | — |

### 5. Синхронизация документации и пилот

Оценить влияние изменений на документацию, внести необходимые обновления и проверить полный процесс на пилотном проекте Private AI Knowledge Platform MVP.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
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
| [IDE-15](https://linear.app/krukov-idea-hub/issue/IDE-15/build-the-manual-implementation-handoff) | Build the manual implementation handoff | Canceled · archived | — | `krukovden/ide-15-build-the-manual-implementation-handoff` | — |
| [IDE-16](https://linear.app/krukov-idea-hub/issue/IDE-16/integrate-github-branches-commits-and-pull-requests) | Integrate GitHub branches, commits, and pull requests | Canceled · archived | — | `krukovden/ide-16-integrate-github-branches-commits-and-pull-requests` | — |
| [IDE-17](https://linear.app/krukov-idea-hub/issue/IDE-17/capture-implementation-verification-and-pr-summaries) | Capture implementation verification and PR summaries | Canceled · archived | — | `krukovden/ide-17-capture-implementation-verification-and-pr-summaries` | — |

### 3. Технический дизайн и планирование

Создать технический дизайн Spike, провести его через утверждение Tech Lead и сформировать задачи реализации.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-69](https://linear.app/krukov-idea-hub/issue/IDE-69/spike-ide-81-design-the-autonomous-technical-design-agent) | [Spike · IDE-81] Design the Autonomous Technical Design Agent | Backlog | Spike | `krukovden/ide-69-spike-ide-81-design-the-autonomous-technical-design-agent` | child of IDE-81, blocks IDE-72 |
| [IDE-72](https://linear.app/krukov-idea-hub/issue/IDE-72/spike-ide-82-design-the-autonomous-task-decomposition-agent) | [Spike · IDE-82] Design the Autonomous Task Decomposition Agent | Backlog | Spike | `krukovden/ide-72-spike-ide-82-design-the-autonomous-task-decomposition-agent` | child of IDE-82 |
| [IDE-81](https://linear.app/krukov-idea-hub/issue/IDE-81/feature-technical-design-agent) | [Feature] Technical Design Agent | Backlog | Feature | `krukovden/ide-81-feature-technical-design-agent` | — |
| [IDE-82](https://linear.app/krukov-idea-hub/issue/IDE-82/feature-task-decomposition-agent) | [Feature] Task Decomposition Agent | Backlog | Feature | `krukovden/ide-82-feature-task-decomposition-agent` | — |
| [IDE-12](https://linear.app/krukov-idea-hub/issue/IDE-12/implement-the-tech-lead-design-approval-gate) | Implement the Tech Lead design approval gate | Canceled · archived | — | `krukovden/ide-12-implement-the-tech-lead-design-approval-gate` | — |
| [IDE-13](https://linear.app/krukov-idea-hub/issue/IDE-13/generate-implementation-tasks-from-the-approved-spike) | Generate implementation tasks from the approved Spike | Canceled · archived | — | `krukovden/ide-13-generate-implementation-tasks-from-the-approved-spike` | — |
| [IDE-14](https://linear.app/krukov-idea-hub/issue/IDE-14/generate-the-spike-technical-design-from-an-approved-feature) | Generate the Spike technical design from an approved Feature | Canceled · archived | — | `krukovden/ide-14-generate-the-spike-technical-design-from-an-approved-feature` | — |

## How to use this file

This is a snapshot. For anything that must be current — a status right now, the full text of an issue, comments, or an approval record — query Linear directly; see `CLAUDE.md`. Use this file for orientation, for offline work, and to see in `git log` how the shape of the work changed over time.
