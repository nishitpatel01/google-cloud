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
        
### Model Factory Development

#### Step 1: Install dependencies
        
        ```
            !pip install joblib google-cloud-documentai ratelimiter tabulate immutabledict
        ```
        
#### Step 2: Create laberer pool

The pool can be reused in the development of multiple processors. 

        ```
            from model_factory import http_client

            LABELER_POOL_DISPLAY_NAME = 'Labeler Pool Name',
            LABELER_POOL_MANAGER_EMAILS = "Labeler Pool Manager email"

            dai_client = http_client.DocumentAIClient()

            lro_name = dai_client.create_labeler_pool(LABELER_POOL_DISPLAY_NAME , LABELER_POOL_MANAGER_EMAILS)
            print('Creating labeler pool...\\nThis could take a few seconds. Please wait.')

            lro = dai_client.wait_for_lro(lro_name)
            if 'response' in lro:
                labeler_pool = lro['response']['name']
                print(f'Labeler pool created: {labeler_pool}')
                else:
                    print(f'Failed to create labeler pool: {lro}')
        ```

After the labeler pool is created, labeler pool managers should receive a email including a link to the manager dashboard for managing labeling tasks and labelers.


#### Step 3: Create processor

Code below will create an Extraction processor. Currently, there are three types of custom processors:

- Custom Document Classifier
- Custom Document Splitter 
- Custom Document Entity Extractor

In this demo, we will be using `Custom Document Entity Extractor` to extract custom information from given documents such as document number and document title

        
            from model_factory import http_client, processor

            
            WORKSPACE = 'gs://<your_bucket_name>/<path_to_the_workspace>'
            new_processor = processor.ExtractionProcessor(WORKSPACE)
            # new_processor = processor.ClassificationProcessor(WORKSPACE) # Use this if you are training for custom classifier
            # new_processor = processor.SplittingProcessor(WORKSPACE)      # Use this if you are training custom splitter
        
        
#### Step 4: Provide schema and labeling instructions

        
        from model_factory import http_client
        from IPython.display import HTML, display
        import tabulate

        dai_client = http_client.DocumentAIClient()
        response = dai_client.list_labeler_pools()

        if 'labelerPools' not in response or not response['labelerPools']:
            print('Labeler pool not found.\\nPlease follow the Prerequisites section to create a labeler pool.')
        else:
            print('Please select one labeler pool from below before running the next code block.')      
            table = [['Display Name', 'Labeler Pool','Managers']]

            for pool in response['labelerPools']:
                table.append([pool['displayName'],pool['name'],', '.join(pool['managerEmails'])])
            display(HTML(tabulate.tabulate(table, tablefmt='html',headers='firstrow')))
        
        
        
            # Replace values below

            LABELER_POOL = 'projects/*/locations/*/labelerPools/*' # Use a labeler pool from the above table

            SCHEMA = {
                    'displayName': 'Schema name',
                    'description': 'Schema description',
                    'entityTypes': [
                        {
                            'type': 'type1',
                            'baseType': 'money',
                            'occurrenceType': 'OPTIONAL_ONCE',
                        },
                        {
                            'type': 'type2',
                            'baseType': 'datetime',
                            'occurrenceType': 'OPTIONAL_ONCE',
                        },
                    ]
                }

            INSTRUCTION_URI = 'gs://<your_bucket_name>/<path_to_the_instruction_pdf>' # PDF instructions to be shared with labeler manager.
            new_processor.update_data_labeling_config(SCHEMA, INSTRUCTION_URI, LABELER_POOL)
        
        
#### Step 5: Import training and test documents

        
            TRAINING_SET_PATH = 'gs://<your_bucket_name>/<path_to_training_set>'
            TEST_SET_PATH = 'gs://<your_bucket_name>/<path_to_test_set>'

            new_processor.import_documents(TRAINING_SET_PATH, 'training')
            new_processor.import_documents(TEST_SET_PATH, 'test')
        
        
#### Step 6: Label documents

After you run the below code block, please go to the labeler manager console to assign the task to corresponding labelers so that they can see the tasks in the UI.

        
            new_processor.label_dataset('training')
            new_processor.label_dataset('test')
        
        
#### Step 7: Train the processor    

This step could take a few hours depending on the size of training and test datasets.

        
            PROCESSOR_VERSION_DISPLAY_NAME = 'Version1' # Use English letters, digits, underscore, hyphen only.

            # If you are training an extraction processor and interested in specifying algorithms. Set active algorithms first:
            # new_processor.active_algorithms = ['eesf', 'clara', 'gbow-flee1', 'harvester']

            # If you'd like to lower min dataset size thresholds from their default values to for example 5, use these options:
            # new_processor.processing_options['min-ground-truth-documents'] = '5'
            # new_processor.processing_options['min-ground-truth-documents-per-entity-type'] = '5'
            # new_processor.processing_options['min-ground-truth-entities-per-entity-type'] = '5'

            new_processor_version = new_processor.train('training','test', display_name = PROCESSOR_VERSION_DISPLAY_NAME)

            print(f'Trained processor version: {new_processor_version}')
            processor_name = '/'.join(new_processor.processor_name().split('/')[2:])
            evaluation_uri = f'https://console.cloud.google.com/ai/document-ai/{processor_name}/evaluations'

            from IPython.core.display import display, HTML
            display(HTML(f'<a href=\"{evaluation_uri}\">Evaluation page</a>'))
        