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