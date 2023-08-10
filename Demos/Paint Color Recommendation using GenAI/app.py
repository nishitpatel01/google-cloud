
# Libraries import
import json 
from typing import List
from itertools import groupby
import pprint


import vertexai
from vertexai.language_models import TextGenerationModel
from google.cloud import discoveryengine

import streamlit as st
import langchain
from pydantic import BaseModel
from langchain.llms.base import LLM
from langchain import PromptTemplate, LLMChain
from langchain.llms import VertexAI


# Project Vars
PROJECT_ID="pnishit-mlai"
LOCATION="global"
SERVING_CONFIG_ID="" # <your-genai-app-builder-chatbot-config-id>
SEARCH_ENGINE_ID="" #<your-genai-app-builder-search-engine-id>


# Recommendation template
recommend_template = """

    The context 

    Make paint color recommendations based on websites like architecturaldigest.com, elledecor.com, dwell.com,
    veranda.com, bhg.com, hgtv.com, apartmenttherapy.com and similar websites. 

    Get the color names and codes from only BenjaminMoore.com, Farrow-Ball.com, and Sherwin-Williams.com  

    Make sure to recommend at least three recommended_colors


    Structure the response as per below defined format

    {format_instructions}

    % USER INPUT:
    {user_input}
    """

# Matched color template
match_template =  """

Get the color names, codes and uri from only BenjaminMoore.com, Farrow-Ball.com, and Sherwin-Williams.com  

Here are combinations of color and brand names

% RECOMMENDED COLOR, RECOMMENDED BRAND
{recommended_brand_name, recommended_color_name}

Provide at least three matches for each combination of color and brand names using below defined format

{format_instructions}
"""


# LLM custom wrapper

class VertexLLMTextExractor(LLM):
    model: TextGenerationModel
    predict_kwargs: dict

    def __init__(self, model, **predict_kwargs):
        super().__init__(model=model, predict_kwargs=predict_kwargs)

    @property
    def _llm_type(self):
        return 'VertexLLM'

    def _call(self, prompt, stop=None):
        result = self.model.predict(prompt, **self.predict_kwargs)
        return str(result)

    @property
    def _identifying_params(self):
        return {}

# Call llm model

model = TextGenerationModel.from_pretrained("text-bison@001")
parameters = {
    "max_output_tokens": 1024,
    "temperature": 0.2,
    "top_k": 40,
    "top_p": 0.8,
}

llm = VertexLLMTextExractor(
  model,
  **parameters
)


# Set of helper functions

# Function to get schema
def get_response_schema(chain: str):
    
    from langchain.output_parsers import StructuredOutputParser, ResponseSchema
    from langchain.prompts import HumanMessagePromptTemplate
    
    # Define recommended color & brand schema
    recommendation_response_schema = [
        ResponseSchema(name="recommended_brand_name", description="recommended brand name from llm output"),
        ResponseSchema(name="recommended_color_name", description="recommended color name from llm output")
    ]
    
    # Format response intructions
    response_schema_output_parser = StructuredOutputParser.from_response_schemas(recommendation_response_schema)
    recommendation_response_format_instructions = response_schema_output_parser.get_format_instructions()
    
    # Define matched color schema
    matches_response_schema = [
        ResponseSchema(name="recommended_brand_name", description="given recommended brand name"),
        ResponseSchema(name="recommended_color_name", description="given recommended color name"),
        ResponseSchema(name="matched_brand_name", description="matched brand name for given recommended_color_name and recommended_brand_name combination"),
        ResponseSchema(name="matched_color_name", description="matched color name for given recommended_color_name and recommended_brand_name combination"),
        ResponseSchema(name="matched_uri", description="color uri of matched color name for given recommended_color_name and recommended_brand_name combination")
    ]
    
    # Format response intructions
    matches_response_schema_output_parser = StructuredOutputParser.from_response_schemas(matches_response_schema)
    matches_response_format_instructions = matches_response_schema_output_parser.get_format_instructions()
    
    if chain == 'recommend':
        return recommendation_response_format_instructions
    elif chain == 'match':
        return matches_response_format_instructions
    else:
        pass
    
        
# Function to generate prompt template
def generate_prompt(chain: str, input_prompt_text: str):
    
    format_intruction = get_response_schema(chain)
    
    if chain == 'recommend':
        
        # Create prompt template
        prompt = PromptTemplate(
            input_variables=["user_input"],
            partial_variables={"format_instructions": format_intruction},
            template=recommend_template
        )

        color_recommendations_promptValue = prompt.format(user_input=input_prompt_text)        
        return prompt
        
    elif chain == 'match':
        matches_prompt = PromptTemplate(
            input_variables=["recommended_brand_name, recommended_color_name"],
            partial_variables={"format_instructions": format_intruction},
            template=match_template
        )
        return matches_prompt

    else:
        return None
 

# Function for enterprise search for color url
def search_sample(
    project_id: str,
    location: str,
    search_engine_id: str,
    serving_config_id: str,
    search_query: str,
) -> List[discoveryengine.SearchResponse.SearchResult]:

    # Create a client
    client = discoveryengine.SearchServiceClient()

    # The full resource name of the search engine serving config
    # e.g. projects/{project_id}/locations/{location}
    serving_config = client.serving_config_path(
        project=project_id,
        location=location,
        data_store=search_engine_id,
        serving_config=serving_config_id,
    )
    
    request = discoveryengine.SearchRequest(
        serving_config=serving_config,
        query=search_query,
        page_size=5,
        content_search_spec=discoveryengine.SearchRequest.ContentSearchSpec(
            snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                max_snippet_count=1
            )
        ),
    )
    
    response_pager = client.search(request)
    
    response = discoveryengine.SearchResponse(
        results=response_pager.results,
        facets=response_pager.facets,
        guided_search_result=response_pager.guided_search_result,
        total_size=response_pager.total_size,
        attribution_token=response_pager.attribution_token,
        next_page_token=response_pager.next_page_token,
        corrected_query=response_pager.corrected_query,
        summary=response_pager.summary,
    )

    response_json = discoveryengine.SearchResponse.to_json(
        response,
        including_default_value_fields=False,
        indent=4,
    )

    return json.loads(response_json) 


# Function to recommend and match colors

def recommend_and_matches(input_prompt_text: str):
    
    import json
    from langchain.chains import LLMChain, SimpleSequentialChain
    
    # Simple sequential chain
    # Holds recommended colors from user input response
    recommended_color_chain = LLMChain(llm=llm, prompt=generate_prompt('recommend', input_prompt_text))

    # Holds matchee colors from recommended colors
    matched_color_chain = LLMChain(llm=llm,prompt=generate_prompt('match', input_prompt_text))
    
    # Build final chain
    overall_chain = SimpleSequentialChain(chains=[recommended_color_chain, matched_color_chain], verbose=False)
    colors = overall_chain.run(input_prompt_text)
    
    json_colors = json.loads(colors.strip('```json```'))
    
    return json_colors
    
    
def get_results(user_input, project_id, location, search_engine_id, serving_config_id):
    
    response = recommend_and_matches(user_input)

    for mt_color in response:
        lst = search_sample(project_id, location, search_engine_id, serving_config_id, mt_color['matched_color_name'])
        
        try:
            color_url = lst['results'][0]['document']['derivedStructData']['pagemap']['metatags'][0]['og:url']
            updated_dict = {'matched_uri': color_url}
            mt_color.update(updated_dict)
        except IndexError:
            color_url = None
            mt_color.update(updated_dict)

    return response   
    

def final_out(user_input, project_id, location, search_engine_id, serving_config_id):
    
    payload = get_results(user_input, PROJECT_ID, LOCATION, SEARCH_ENGINE_ID, SERVING_CONFIG_ID)
    grouped = [{key: list(group) for key, group in groupby(payload, key=lambda x: x["recommended_color_name"])}]

    return grouped


gen_ai_options = st.selectbox(
    "Select an Option",
    ["User Input"]
)

with st.sidebar:
    pass

if gen_ai_options == "User Input":
    text = st.text_area(label="Enter text", height=500)
    if text:
        group_dict = final_out(text, PROJECT_ID, LOCATION, SEARCH_ENGINE_ID, SERVING_CONFIG_ID)
        
        st.text_area(label="Summary results",  value=group_dict, height=300)




