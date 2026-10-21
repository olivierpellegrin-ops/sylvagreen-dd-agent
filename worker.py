# Agent DD Ingest — SylvaGreen Capital
# Worker : enregistre le workflow "dd-overview-ingest" auprès de Mistral Workflows.
import mistralai.workflows as workflows

from dd_ingest import DDOverviewIngestWorkflow


def main() -> None:
    workflows.run_worker([DDOverviewIngestWorkflow])


if __name__ == "__main__":
    main()
