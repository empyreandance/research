// Where the ingest data lives.
//
// In local development we read the sample cycle generated into ./.devdata.
// In production set DATA_BASE_URL to your R2 bucket's PUBLIC url — either the
// managed r2.dev domain ("https://pub-<hash>.r2.dev") or a custom domain you
// connect ("https://data.example.com"). It is NOT the S3 API endpoint
// (*.r2.cloudflarestorage.com); that one requires signed requests and will 401.
// R2 must allow this site's origin via CORS (see runbook: "R2 public access").
//
// The page reads <DATA_BASE_URL>/manifest.json to find the current cycle.

const isLocalhost = ["localhost", "127.0.0.1"].includes(location.hostname);

// Must be ABSOLUTE: the Zarr reader's FetchStore builds `new URL(...)` internally,
// which rejects relative paths. In dev we resolve .devdata against the page URL.
export const DATA_BASE_URL = isLocalhost
  ? new URL(".devdata", document.baseURI).href.replace(/\/+$/, "")
  : "https://hrrr-data.alexcooke.co";
