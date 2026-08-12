<!-- GENERATED FILE - DO NOT EDIT.
     Regenerate with: python3 scripts/sync_linear_state.py
     Linear is the source of truth; this file is a mirror. -->

# AI SDLC Platform — project state

**Generated:** 2026-08-12T22:43:18Z
**Source:** [https://linear.app/krukov-idea-hub/project/ai-sdlc-platform-ba96723ef010](https://linear.app/krukov-idea-hub/project/ai-sdlc-platform-ba96723ef010)
**Project status:** Planned
**Issues:** 6 live (1 in progress, 1 done) · 15 archived

## Project documents

- [00 · HUB — read this before any work](https://linear.app/krukov-idea-hub/document/00-hub-read-this-before-any-work-4d61e3161927)
- [Конституция и видение проекта](https://linear.app/krukov-idea-hub/document/konstituciya-i-videnie-proekta-7f92af685fc1)
- [Референсная архитектура](https://linear.app/krukov-idea-hub/document/referensnaya-arhitektura-951bc7c33b59)

## Milestones

### 2. Исследование фичи

Создать локальный skill, который исследует идею, координирует независимую проверку LLM, формирует утверждённые артефакты Feature и публикует их в Linear.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-68](https://linear.app/krukov-idea-hub/issue/IDE-68/spike-design-the-deterministic-feature-discovery-skill) | [Spike] Design the deterministic Feature Discovery Skill | In Progress | Spike | `krukovden/ide-68-spike-design-the-deterministic-feature-discovery-skill` | blocks IDE-69 |
| [IDE-10](https://linear.app/krukov-idea-hub/issue/IDE-10/add-grilling-research-and-independent-llm-review) | Add grilling, research, and independent LLM review | Canceled · archived | — | `krukovden/ide-10-add-grilling-research-and-independent-llm-review` | — |
| [IDE-11](https://linear.app/krukov-idea-hub/issue/IDE-11/generate-and-publish-the-approved-feature-package) | Generate and publish the approved Feature package | Canceled · archived | — | `krukovden/ide-11-generate-and-publish-the-approved-feature-package` | — |
| [IDE-9](https://linear.app/krukov-idea-hub/issue/IDE-9/build-the-local-feature-discovery-skill-entry-point) | Build the local Feature Discovery skill entry point | Canceled · archived | — | `krukovden/ide-9-build-the-local-feature-discovery-skill-entry-point` | — |

### 1. Фундамент и контракты

Создать связанный фундамент проекта в Linear и GitHub, определить терминологию платформы, контракты артефактов и модель конфигурации проекта.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-71](https://linear.app/krukov-idea-hub/issue/IDE-71/spike-define-linear-workflow-states-approvals-and-agent-handoffs) | [Spike] Define Linear workflow states, approvals, and agent handoffs | Backlog | Spike | `krukovden/ide-71-spike-define-linear-workflow-states-approvals-and-agent` | blocks IDE-72, blocks IDE-69, related IDE-68 |
| [IDE-76](https://linear.app/krukov-idea-hub/issue/IDE-76/spike-design-the-project-memory-contract) | [Spike] Design the Project Memory contract | Backlog | Spike | `krukovden/ide-76-spike-design-the-project-memory-contract` | related IDE-68, related IDE-71 |
| [IDE-5](https://linear.app/krukov-idea-hub/issue/IDE-5/establish-the-linked-linear-and-github-project-foundation) | Establish the linked Linear and GitHub project foundation | Done | — | `krukovden/ide-5-establish-the-linked-linear-and-github-project-foundation` | related IDE-76 |
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

### 4. Реализация и поставка

Обеспечить ручную передачу в реализацию, интеграцию с репозиторием, тестирование и процесс pull request.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-15](https://linear.app/krukov-idea-hub/issue/IDE-15/build-the-manual-implementation-handoff) | Build the manual implementation handoff | Canceled · archived | — | `krukovden/ide-15-build-the-manual-implementation-handoff` | — |
| [IDE-16](https://linear.app/krukov-idea-hub/issue/IDE-16/integrate-github-branches-commits-and-pull-requests) | Integrate GitHub branches, commits, and pull requests | Canceled · archived | — | `krukovden/ide-16-integrate-github-branches-commits-and-pull-requests` | — |
| [IDE-17](https://linear.app/krukov-idea-hub/issue/IDE-17/capture-implementation-verification-and-pr-summaries) | Capture implementation verification and PR summaries | Canceled · archived | — | `krukovden/ide-17-capture-implementation-verification-and-pr-summaries` | — |

### 3. Технический дизайн и планирование

Создать технический дизайн Spike, провести его через утверждение Tech Lead и сформировать задачи реализации.

| Issue | Title | Status | Labels | Branch | Links |
|---|---|---|---|---|---|
| [IDE-69](https://linear.app/krukov-idea-hub/issue/IDE-69/spike-design-the-autonomous-technical-design-agent) | [Spike] Design the Autonomous Technical Design Agent | Backlog | Spike | `krukovden/ide-69-spike-design-the-autonomous-technical-design-agent` | blocks IDE-72 |
| [IDE-72](https://linear.app/krukov-idea-hub/issue/IDE-72/spike-design-the-autonomous-task-decomposition-agent) | [Spike] Design the Autonomous Task Decomposition Agent | Backlog | Spike | `krukovden/ide-72-spike-design-the-autonomous-task-decomposition-agent` | — |
| [IDE-12](https://linear.app/krukov-idea-hub/issue/IDE-12/implement-the-tech-lead-design-approval-gate) | Implement the Tech Lead design approval gate | Canceled · archived | — | `krukovden/ide-12-implement-the-tech-lead-design-approval-gate` | — |
| [IDE-13](https://linear.app/krukov-idea-hub/issue/IDE-13/generate-implementation-tasks-from-the-approved-spike) | Generate implementation tasks from the approved Spike | Canceled · archived | — | `krukovden/ide-13-generate-implementation-tasks-from-the-approved-spike` | — |
| [IDE-14](https://linear.app/krukov-idea-hub/issue/IDE-14/generate-the-spike-technical-design-from-an-approved-feature) | Generate the Spike technical design from an approved Feature | Canceled · archived | — | `krukovden/ide-14-generate-the-spike-technical-design-from-an-approved-feature` | — |

## How to use this file

This is a snapshot. For anything that must be current — a status right now, the full text of an issue, comments, or an approval record — query Linear directly; see `CLAUDE.md`. Use this file for orientation, for offline work, and to see in `git log` how the shape of the work changed over time.
