# Test setup artifact, not benchmark evidence

The old offline rejection test read the production configuration after it was switched to live, so it constructed a runner and wrote this incomplete synthetic-1 manifest. Engine.run was never called; no API request was dispatched (ledger still contains the original 3 requests). This artifact was moved out of results/runs to prevent accidental inclusion in analysis. The test now uses a temporary offline configuration and a credential-read sentinel.
