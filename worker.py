# Agent DD Ingest — SylvaGreen Capital
# Worker : enregistre les workflows auprès de Mistral Workflows.
import mistralai.workflows as workflows

from dd_ingest import DDOverviewIngestWorkflow
from zoho_fields import ZohoMetaWorkflow


def main() -> None:
    workflows.run_worker([DDOverviewIngestWorkflow, ZohoMetaWorkflow])


if __name__ == "__main__":
    main()
