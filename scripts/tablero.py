import pandas as pd
import streamlit as st

from services.db import get_connection


def cargar_datos():
    """Obtiene los mensajes y calcula la cantidad de palabras por chat."""
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT numero, mensaje FROM mensajes")
    datos = cur.fetchall()
    conn.close()

    df = pd.DataFrame(datos, columns=["numero", "mensaje"])
    df["palabras"] = df["mensaje"].fillna("").apply(lambda x: len(x.split()))
    return df.groupby("numero")['palabras'].sum().reset_index()


def main():
    st.title("Número de palabras por chat")
    df = cargar_datos()
    st.bar_chart(df.set_index('numero'))


if __name__ == "__main__":
    main()
