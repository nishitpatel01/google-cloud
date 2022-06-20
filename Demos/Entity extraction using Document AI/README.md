# Custom Entity Extraction using Document AI

### Document AI is a document understanding solution that takes unstructured data and makes the data easier to understand, analyze, and consume by providing structure through content classification, custom entity extraction, advanced searching, and more.

Using Document AI, you can:

- Convert images to text
- Classify documents
- Analyze and extract entities within documents

#### Document AI provided several general purpose build processor as well as specialized processors like lending AI, Procedurement AI and Identity processors to work with most commonly used document types. Document AI also offered custom processors for your use case that can not be worked with general purpose and specialized processors. The notebooks in this repo shows how to build and train custom processor and use it to extract custom entities from documents. 


### Setup 

- Enable Document API in your Google cloud project
    - Google Cloud Console > API Library Page > Cloud Document AI API
        ```
            gcloud services enable documentai.googleapis.com
        ```
        
- Setup Authentication
    - Create a `service account`
        ```
            gcloud iam service-accounts create <SERVICE_ACCOUNT_ID> \
            --description="DESCRIPTION" \
            --display-name="DISPLAY_NAME"
        ```
    - Grant service account `Document AI Owner` role
        ```
            gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${SVC_ACCOUNT}"  \
            --role=roles/documentai.owner 
        ```
        
        
        