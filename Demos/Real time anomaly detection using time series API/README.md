# Detect anomalies in real time using time series insight API

### Time Series Insight API is serverless and fully managed API and can be used for large scale forecasting and supports real time anomaly detection with low latency. It handles dataset consisting of billions of events and supports thousands of queries per second. Users can detect trends and anomalies with multiple event dimensions.

### Problem Desciption

In this demo, we will use an artificial tabular dataset (in time series format) to detect the anomalies across different dimension. The dataset consists of few dimensions and each row in the dataset represents an event which is real time stream coming from a sensor. Each sensor event is a reading from a physical room containing the temperature, humidity, light and hydrogen value. The data is streamed every 23 seconds or so. In total, dataset has approximately 202K rows for 2 months readings. 

We will use this datase with `Time Series Insight API` to detect anomalies using dimension temperature and light values.

### Setup 

- Enable Time Series API
    - Google Cloud Console > API Library Page > Time Series API
        ``` 
            gcloud services enable timeseriesinsights.googleapis.com
        ``` 
- Authorization
    - Create a `service account`
        ```
            gcloud iam service-accounts create <SERVICE_ACCOUNT_ID> \
            --description="DESCRIPTION" \
            --display-name="DISPLAY_NAME"
        ```
    - Grant service account `Timeseries Insights DataSet Owner` role
        ```
            gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${SVC_ACCOUNT}"  \
            --role=roles/timeseriesinsights.datasetsOwner 
        ```
        
### Dataset

As mentioned in the problem descrition, we have used an artificial dataset containing the few dimensions for each event. Some sample rows from the dataset looks like below. This is a table in the Bigquery

![TSI Tabular Data](../../data/images/TSI-tabular-sample.png)

Here are the values for each dimension change over time

![TSI Chart](../../data/images/TSI-chart.png)

We will use this data to perform anonmaly detection on dimesion `Temperature` and `Light`. But first, we will need to convert this tabular data into another jsonl form so that API can consume it. Because all these data dimensions are of numerical value, it is important for our use case to insert a “dummy” categorical string based dimension on our data, so that we could query on that “slice”. Currently, the only way to create a dataset is from a Cloud Storage object. This Cloud Storage object must be a JSON file with all the event data as a JSON object with each data point on a new line (this is sometimes known as JSONL). The data must also be structured in the form the API is expecting for an event which can be seen [here](https://cloud.google.com/timeseries-insights/docs/reference/rest/v1/projects.datasets/appendEvents#Event).


The converted jsonl event looks like below:
    ```
        {"groupId":"2583958225776393023",
            "eventTime":"2021-06-14T00:00:04+00:00",
            "Dimensions":[
                {"name":"measure","stringVal":"LTTH"},
                {"name":"Humidity","doubleVal":36},
                {"name":"Light","doubleVal":99},
                {"name":"h2_raw","doubleVal":1041},
                {"name":"temp","doubleVal":36.97}
        ]}

    ```



