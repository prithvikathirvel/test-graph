import os
import logging
from pydantic import BaseModel, Field
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from google.oauth2 import service_account
from langchain_google_genai import ChatGoogleGenerativeAI

from app.engine.registry import NodeRegistry
from app.utils.templating import resolve_placeholders
from app.core.state import FlowState

logger = logging.getLogger(__name__)

credentials = service_account.Credentials.from_service_account_file(
    "service-account.json",
    scopes=["https://www.googleapis.com/auth/cloud-platform"],
)

class ClassificationResult(BaseModel):
    category: str = Field(description="The exact label of the category selected.")
    confidence: float = Field(description="Confidence score between 0.0 and 1.0")

@NodeRegistry.register("Question Classifier")
async def classifier_node(state: FlowState, node_config: dict) -> dict:
    """Uses LLMs to strictly classify user queries based on UI categories."""
    
    inputs = {p["key"]: p["value"] for p in node_config.get("inputParameters", [])}
    
    user_query = resolve_placeholders(inputs.get("input_text", ""), state["variables"])
    classifications = inputs.get("classifications", [])
    model_choice = inputs.get("model", "Gemini")
    instructions = inputs.get("instructions", "Classify the user's intent.")
    
    out_params = node_config.get("outputParameters", [])
    output_key = out_params[0]["value"] if out_params else "question_category"

    # Build the classification options into a strict string for the LLM
    categories_str = "\n".join([f"- {c['label']}: {c['description']} (Keywords: {', '.join(c.get('keywords', []))})" for c in classifications])

    system_prompt = (
        f"{instructions}\n\n"
        f"You must classify the user's input into EXACTLY ONE of these categories:\n"
        f"{categories_str}\n\n"
        f"If none apply, pick the closest match or a generic category if available."
    )

    # Instantiate Model
    if "Gemini" in model_choice:
        llm = ChatGoogleGenerativeAI(
                credentials=credentials,
                model="gemini-2.5-flash",
                temperature=0,
               # convert_system_message_to_human=True
            )
    else:
        llm = ChatOpenAI(
            model="meta/llama-3.3-70b-instruct",
            api_key="sk-Fm3dP1vX7qYt6uJzZbL5Kr2HgS8oWnCxEjQaRfNiGpTl",
            base_url="https://infinitai.sifymdp.digital/maas/v1"
        )

    try:
        # Force the LLM to output the exact JSON structure defined in ClassificationResult
        structured_llm = llm.with_structured_output(ClassificationResult)
        
        prompt = ChatPromptTemplate.from_messages([
            ("system", system_prompt),
            ("human", "{user_query}")
        ])
        
        chain = prompt | structured_llm
        logger.info(f"Running Classifier on: {user_query}")
        
        result: ClassificationResult = await chain.ainvoke({"user_query": user_query})
        
        return {"variables": {output_key: result.category, f"{output_key}_confidence": result.confidence}}
        
    except Exception as e:
        logger.error(f"Classifier error: {e}")
        return {"variables": {output_key: classifications[0]["label"] if classifications else "Unknown"}}