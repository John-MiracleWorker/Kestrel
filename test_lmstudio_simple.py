import openai

client = openai.OpenAI(base_url="http://127.0.0.1:1234/v1", api_key="not-needed")

try:
    response = client.chat.completions.create(
        model="google/gemma-4-26b-a4b",
        messages=[{"role": "user", "content": "Say hello in one word."}],
        max_tokens=10,
    )
    print("Response:", response.choices[0].message.content)
except Exception as e:
    print("Error:", type(e).__name__, e)
