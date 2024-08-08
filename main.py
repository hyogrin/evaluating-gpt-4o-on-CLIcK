import os
import time
import argparse

import openai
from openai import AzureOpenAI, RateLimitError
import pandas as pd
from tqdm import tqdm
from dotenv import load_dotenv
from datasets import load_dataset

from prompts import TYPE_1, TYPE_2, TYPE_3, TYPE_4
from langchain_openai import AzureChatOpenAI
from langchain_core.pydantic_v1 import BaseModel, Field
from langchain.schema.output_parser import StrOutputParser
from langchain_core.prompts import PromptTemplate, ChatPromptTemplate, HumanMessagePromptTemplate, SystemMessagePromptTemplate
from langchain_openai import AzureChatOpenAI
from logger import logger


def format_timespan(seconds):
    hours = seconds // 3600
    minutes = (seconds - hours*3600) // 60
    remaining_seconds = seconds - hours*3600 - minutes*60
    timespan = f"{hours} hours {minutes} minutes {remaining_seconds:.4f} seconds."
    return timespan


class CustomStrOutputParser(StrOutputParser):
    def parse(self, text: str) -> str:
        response = text.strip().replace('"', "").replace("'", "")
        if response.startswith("A"):
            pred = "A"
        elif response.startswith("B"):
            pred = "B"
        elif response.startswith("C"):
            pred = "C"
        elif response.startswith("D"):
            pred = "D"
        elif response.startswith("E"):
            pred = "E"
        else:
            pred = ""  # Wrong answer

        return pred, response


def get_prompt(x) -> str:
    num_choices = len(x["choices"])
    if num_choices == 4:
        if x["paragraph"] != "":  # Use Type 1 Prompt
            return TYPE_1.format(
                CONTEXT=x["paragraph"],
                QUESTION=x["question"],
                A=x["choices"][0],
                B=x["choices"][1],
                C=x["choices"][2],
                D=x["choices"][3],
            )
        else:
            return TYPE_2.format(
                QUESTION=x["question"],
                A=x["choices"][0],
                B=x["choices"][1],
                C=x["choices"][2],
                D=x["choices"][3],
            )
    elif num_choices == 5:
        if x["paragraph"] != "":
            return TYPE_3.format(
                CONTEXT=x["paragraph"],
                QUESTION=x["question"],
                A=x["choices"][0],
                B=x["choices"][1],
                C=x["choices"][2],
                D=x["choices"][3],
                E=x["choices"][4],
            )
        else:
            return TYPE_4.format(
                QUESTION=x["question"],
                A=x["choices"][0],
                B=x["choices"][1],
                C=x["choices"][2],
                D=x["choices"][3],
                E=x["choices"][4],
            )
    else:
        raise ValueError(f"Invalid number of choices: {num_choices} (ID: {x['id']})")


def get_prompt_template():
    system_prompt = "You are an AI assistant who reads a given question and solves multiple choice questions."
    system_message_template = SystemMessagePromptTemplate.from_template(system_prompt)
    human_prompt = [
        {
            "type": "text",
            "text": "{question}"
        },
    ]
    human_message_template = HumanMessagePromptTemplate.from_template(human_prompt)

    prompt = ChatPromptTemplate.from_messages(
        [
            system_message_template,
            human_message_template
        ]
    )
    return prompt


def get_answer(x) -> str:
    # 왜 이렇게 .strip() 처리를 해주었는지는 README에 issue 파트 참고 부탁드립니다.
    answer_idx = [xx.strip() for xx in x["choices"]].index(x["answer"].strip())
    if answer_idx == -1:
        raise ValueError(f"Answer not found in choices: {x['answer']} (ID: {x['id']})")
    return chr(0x41 + answer_idx)  # answer_idx = 0 -> answer = "A"


def benchmark(args):

    IS_DEBUG = args.is_debug
    MAX_RETRIES = args.max_retries
    DELAY_INCREMENT = 30
    MODEL_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")

    num_debug_samples = args.num_debug_samples
    batch_size = args.batch_size
    max_tokens = args.max_tokens
    temperature = args.temperature

    llm = AzureChatOpenAI(
        temperature=temperature, 
        max_tokens=max_tokens,
        openai_api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
        azure_deployment=MODEL_NAME,                   
    )

    click_ds = load_dataset("EunsuKim/CLIcK")["train"]

    if IS_DEBUG:
        click_ds = click_ds.select(range(num_debug_samples))

    all_batch = [{"id": x["id"], "question": get_prompt(x), "answer": get_answer(x)} for x in tqdm(click_ds)]
    responses = []
    prompt_template = get_prompt_template()
    chain = prompt_template | llm | CustomStrOutputParser()

    logger.info(f"====== [START] Generate answers to questions given by LLM. =====")
    t0 = time.time()

    with tqdm(total=len(all_batch), desc="Processing Answers") as pbar:

        for i in range(0, len(all_batch), batch_size):
            mini_batch = all_batch[i:i+batch_size]
            retries = 0
            
            while retries <= MAX_RETRIES:
                try:
                    preds = chain.batch(mini_batch, {"max_concurrency": batch_size})
                    # If no exception, add questions and answers to all_answers
                    for qna, pred in zip(mini_batch, preds):
                        responses.append({"id": qna["id"], "trial": 0, "answer": qna["answer"], "pred": pred[0], "response": pred[1]})
                    break  # Exit the retry loop once successful
                except RateLimitError as rate_limit_error:
                    delay = (retries + 1) * DELAY_INCREMENT
                    logger.warning(f"{rate_limit_error}. Retrying in {delay} seconds...")
                    time.sleep(delay)
                    retries += 1

                    if retries > MAX_RETRIES:
                        logger.error(f"Max retries reached this batch. Skipping to next batch.")
                        break
                except openai.BadRequestError as e:
                    logger.error(f"BadRequestError: {e}. Skipping this batch.")
                    break
                except Exception as e:
                    logger.error(f"Error in process_inputs: {e}")
                    break            
            
            pbar.set_postfix({"current_batch": f"{i//batch_size + 1}/{(len(all_batch) + (batch_size-1))//batch_size}"})
            pbar.update(len(mini_batch))

    t1 = time.time()
    timespan = format_timespan(t1 - t0)
    logger.info(f"===== [DONE] Generating Answer dataset took {timespan}")

    df = pd.DataFrame(responses)
    os.makedirs("results", exist_ok=True)
    csv_path = f"results/{MODEL_NAME}.csv"
    df.to_csv(csv_path, index=False)

    logger.info(f"====== [START] Evluation start - CSV_PATH: {csv_path} =====")
    evaluate(csv_path)
    logger.info(f"====== [START] Evluation end =====")


def evaluate(csv_path="results/gpt-4o-mini.csv"):
    # Please clone the CLIcK repository(https://github.com/rladmstn1714/CLIcK/) to the same directory as this repository.
    import glob
    import json
    import pandas as pd

    #result = pd.read_csv("results/gpt-4o-2024-05-13.csv")
    result = pd.read_csv("results/gpt-4o-mini.csv")

    # print(result.head())

    file_dict = {
        "History": "CLIcK/Dataset/Culture/Korean History",
        "Geography": "CLIcK/Dataset/Culture/Korean Geography",
        "Law": "CLIcK/Dataset/Culture/Korean Law",
        "Politics": "CLIcK/Dataset/Culture/Korean Politics",
        "Society": "CLIcK/Dataset/Culture/Korean Society",
        "Tradition": "CLIcK/Dataset/Culture/Korean Tradition",
        "Economy": "CLIcK/Dataset/Culture/Korean Economy",
        "Pop Culture": "CLIcK/Dataset/Culture/Korean Popular",
        "Textual": "CLIcK/Dataset/Language/Textual",
        "Functional": "CLIcK/Dataset/Language/Functional",
        "Grammar": "CLIcK/Dataset/Language/Grammar",
    }

    id_to_category = {}

    for category, dir_path in file_dict.items():
        file_paths = glob.glob(f"{dir_path}/*.json")
        for file_path in file_paths:
            with open(file_path, "r") as f:
                data = json.loads(f.read())
                for x in data:
                    id_to_category[x["id"]] = category

    result["category"] = result["id"].map(id_to_category)
    result["correct"] = result["answer"] == result["pred"]
    print(result.groupby(["category"])["correct"].agg(["mean", "count"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Options')
    parser.add_argument("--is_debug", type=bool, default=False)
    parser.add_argument("--num_debug_samples", type=int, default=10)
    parser.add_argument("--batch_size", type=int, default=10)
    parser.add_argument("--max_retries", type=int, default=3)
    parser.add_argument("--max_tokens", type=int, default=256)
    parser.add_argument("--temperature", type=float, default=0.0)

    args = parser.parse_args()

    logger.info(args)
    benchmark(args)