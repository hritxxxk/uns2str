# Phase 5 & 6 — PIM API Integration (planned, not implemented)

## Phase 5: Programmatic Template Retrieval & Modification Node

**Goal:** VinGPT fetches blank PIM templates using the client's JWT, decodes S3 URLs, and writes normalized data into them.

### Tasks

1. **Implement Programmatic Template Download**
   - Create a node that sends a POST request with the user's JWT to:
     `https://uat-api.vinpim.com/api/pie/v1/download/download-template`

2. **Resolve S3 URL Encoding**
   - Decode the JSON response
   - Replace escaped Unicode sequence `\u0026` with standard `&` so S3 permits the template download

3. **Build the Template Population Node**
   - Write the normalized product mappings directly into the downloaded standard `.xlsx` files using pandas/openpyxl

---

## Phase 6: Automated API S3 Ingestion Node

**Goal:** Automated S3 upload handshake to POST populated templates directly back to the PIM backend.

### Tasks

1. **Call getUrl for Presigned Upload URL**
   - Have the final node call the PIM's `getUrl` endpoint using the user's JWT to request a pre-signed S3 PUT URL for the four populated templates

2. **Perform the S3 PUT Upload**
   - Execute an asynchronous HTTP PUT request to upload compiled binary files directly to the S3 bucket

3. **Trigger Database Ingestion**
   - Send a final POST request to the PIM's `https://uat-api.vinpim.com/api/pie/v1/upload/upload` endpoint
   - Pass the newly uploaded S3 file keys
   - Triggers the final database ingest

4. **Stream the Success Message**
   - Update the chat interface to inform the user that their data is live in the Product Master
