"""
Motor de Creación y Continuación Literaria de POETA (agent/poetry_engine.py).

Responsabilidades:
- Construir prompts literarios estructurados en español para la generación inicial
  (Minuto 00: título, temática y primera estrofa) y para las intervenciones
  posteriores (Minutos 10, 20, 30, 40 y 50: estrofas 2 a 6).
- Incorporar la memoria real recuperada desde PostgreSQL (título, temática,
  número de intervención, estrofas previas y arco dramático/lírico).
- Preparar un desenlace coherente en la sexta y última intervención (Minuto 50).
- Validar estrictamente la respuesta del modelo (Escenario D):
  - Rechazar contenido vacío o truncado.
  - Filtrar comentarios externos o meta-texto fuera del poema.
  - Impedir que el modelo sustituya todo el poema cuando solo debe agregar una estrofa.
  - Unir de forma determinista la nueva estrofa con el texto acumulado anterior.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from api.ollama_client import LLMGenerationResult, LLMProvider


class DefectivePoemResponseError(ValueError):
    """Excepción lanzada cuando la respuesta del modelo incumple las reglas literarias o estructurales."""


@dataclass(frozen=True)
class InitialPoemDraft:
    """Resultado validado de la primera intervención (Minuto 00)."""

    title: str
    theme: str
    first_stanza: str
    full_text: str
    model_name: str
    prompt_snapshot: str
    duration_ms: int


@dataclass(frozen=True)
class StanzaContinuationDraft:
    """Resultado validado de una intervención de continuación (Intervenciones 2 a 6)."""

    intervention_number: int
    added_stanza: str
    full_text: str
    model_name: str
    prompt_snapshot: str
    duration_ms: int


SYSTEM_LITERARY_PROMPT = (
    "Eres POETA, un escritor y poeta literario hispanoamericano de alta sensibilidad estética. "
    "Tu misión es componer una obra poética original en español a lo largo de seis intervenciones. "
    "Mantienes una voz lírica coherente, imágenes evocadoras, ritmo cuidado y progresión temática. "
    "Nunca incluyes saludos, explicaciones técnicas, notas al pie ni comentarios externos a la obra."
)

INTERVENTION_ARC_GUIDANCE: dict[int, str] = {
    1: "Apertura lírica: establece el tono, las imágenes fundacionales y la atmósfera central del poema.",
    2: "Segunda estrofa (Minuto 10): expande la imagen inicial y profundiza en el motivo central sin repetir versos previos.",
    3: "Tercera estrofa (Minuto 20): introduce una tensión simbólica, contraste o transformación emocional coherente con la temática.",
    4: "Cuarta estrofa (Minuto 30): alcanza el núcleo contemplativo o clímax lírico de la composición.",
    5: "Quinta estrofa (Minuto 40): encamina los símbolos poéticos hacia su madurez y prepara la transición al cierre.",
    6: "Sexta y última estrofa (Minuto 50): construye un desenlace coherente, memorable y definitivo que cierre el poema con plenitud estética.",
}


class PoetryEngine:
    """Motor de generación, validación y ensamblaje literario desacoplado de la base de datos."""

    def __init__(self, llm_provider: LLMProvider) -> None:
        self.llm_provider = llm_provider

    def generate_initial_composition(self, seed_theme: str | None = None) -> InitialPoemDraft:
        """
        Ejecuta la Intervención 1 (Minuto 00): solicita al modelo de IA un título original,
        una temática clara y la primera estrofa del poema.
        """
        theme_directive = (
            f"Desarrolla el poema alrededor de la siguiente inspiración temática: '{seed_theme.strip()}'."
            if seed_theme and seed_theme.strip()
            else "Elige libremente una temática poética profunda (la memoria, el tiempo, el cosmos, la naturaleza, la vigilia, el silencio o la creación)."
        )

        user_prompt = (
            "Inicia un nuevo ciclo de creación poética (Intervención 1 de 6 — Minuto 00).\n"
            f"{theme_directive}\n\n"
            "Debes responder ÚNICAMENTE con el siguiente formato estructurado exacto en español, "
            "sin comentarios adicionales antes ni después:\n\n"
            "TITULO: <título original y evocador del poema>\n"
            "TEMATICA: <descripción breve de la temática y tono lírico en 1 oración>\n"
            "ESTROFA:\n"
            "<escribe únicamente la primera estrofa, de 4 a 6 versos en español>"
        )

        result: LLMGenerationResult = self.llm_provider.generate_text(
            system_prompt=SYSTEM_LITERARY_PROMPT,
            user_prompt=user_prompt,
            temperature=0.78,
        )

        title, theme, first_stanza = self._parse_and_validate_initial_response(result.content)

        return InitialPoemDraft(
            title=title,
            theme=theme,
            first_stanza=first_stanza,
            full_text=first_stanza,
            model_name=result.model_name,
            prompt_snapshot=result.prompt_snapshot,
            duration_ms=result.duration_ms,
        )

    def generate_continuation_stanza(
        self,
        *,
        title: str,
        theme: str,
        previous_full_text: str,
        previous_stanzas: list[str],
        intervention_number: int,
        total_interventions: int = 6,
    ) -> StanzaContinuationDraft:
        """
        Ejecuta una intervención posterior (2 a 6) utilizando la memoria real recuperada
        desde PostgreSQL y une la nueva estrofa al poema acumulado.
        """
        if intervention_number < 2 or intervention_number > total_interventions:
            raise ValueError(
                f"El número de intervención de continuación debe estar entre 2 y {total_interventions} "
                f"(recibido: {intervention_number})."
            )
        if not previous_full_text or not previous_full_text.strip():
            raise DefectivePoemResponseError(
                "No es posible continuar el poema porque la memoria recuperada está vacía."
            )

        arc_instruction = INTERVENTION_ARC_GUIDANCE.get(
            intervention_number,
            "Continúa el desarrollo progresivo de la idea central.",
        )

        numbered_memory = "\n\n".join(
            f"[Estrofa {idx}]\n{stanza.strip()}"
            for idx, stanza in enumerate(previous_stanzas, start=1)
        )

        user_prompt = (
            f"Continuación de creación poética — Intervención {intervention_number} de {total_interventions}.\n\n"
            "=== MEMORIA PERSISTENTE DEL POEMA ===\n"
            f"Título: {title}\n"
            f"Temática: {theme}\n"
            f"Estrofas completadas hasta ahora ({len(previous_stanzas)} de {total_interventions}):\n\n"
            f"{numbered_memory}\n"
            "=====================================\n\n"
            f"Directriz literaria para esta intervención:\n{arc_instruction}\n\n"
            "REGLAS ESTRICTAS DE RESPUESTA:\n"
            f"1. Escribe ÚNICAMENTE la estrofa número {intervention_number} (de 4 a 6 versos en español).\n"
            "2. NO repitas el título, la temática ni ninguna de las estrofas anteriores.\n"
            "3. NO incluyas encabezados como 'Estrofa X:' ni explicaciones o comentarios externos.\n"
            "4. Mantén la coherencia métrica, simbólica y lírica con los versos anteriores."
        )

        result: LLMGenerationResult = self.llm_provider.generate_text(
            system_prompt=SYSTEM_LITERARY_PROMPT,
            user_prompt=user_prompt,
            temperature=0.75,
        )

        cleaned_stanza = self._parse_and_validate_continuation_stanza(
            raw_content=result.content,
            title=title,
            previous_stanzas=previous_stanzas,
        )

        updated_full_text = self.combine_poem_text(previous_full_text, cleaned_stanza)

        return StanzaContinuationDraft(
            intervention_number=intervention_number,
            added_stanza=cleaned_stanza,
            full_text=updated_full_text,
            model_name=result.model_name,
            prompt_snapshot=result.prompt_snapshot,
            duration_ms=result.duration_ms,
        )

    @staticmethod
    def combine_poem_text(previous_full_text: str, new_stanza: str) -> str:
        """Une de manera inmutable y limpia el texto acumulado anterior con la nueva estrofa."""
        base = (previous_full_text or "").strip()
        addition = (new_stanza or "").strip()
        if not base:
            return addition
        return f"{base}\n\n{addition}"

    def _parse_and_validate_initial_response(self, raw_content: str) -> tuple[str, str, str]:
        """
        Extrae y valida TITULO, TEMATICA y ESTROFA de la generación inicial.
        Soporta tanto el formato de etiquetas como JSON si el modelo lo devuelve en JSON.
        """
        if not raw_content or not raw_content.strip():
            raise DefectivePoemResponseError("La respuesta inicial del modelo está vacía.")

        text = self._strip_code_fences(raw_content.strip())

        # Caso 1: Respuesta en formato JSON
        if text.startswith("{") and text.endswith("}"):
            try:
                parsed = json.loads(text)
                title = str(parsed.get("titulo") or parsed.get("title") or "").strip()
                theme = str(parsed.get("tematica") or parsed.get("theme") or "").strip()
                stanza = str(parsed.get("estrofa") or parsed.get("stanza") or "").strip()
                if title and theme and stanza:
                    stanza_clean = self._validate_single_stanza(stanza)
                    return title.strip("\"'"), theme.strip("\"'"), stanza_clean
            except ValueError:
                pass

        # Caso 2: Formato etiquetado TITULO / TEMATICA / ESTROFA
        title_match = re.search(
            r"(?:T[ÍI]TULO|TITLE)\s*:\s*(.+)",
            text,
            flags=re.IGNORECASE,
        )
        theme_match = re.search(
            r"(?:TEM[ÁA]TICA|TEMA|THEME)\s*:\s*(.+)",
            text,
            flags=re.IGNORECASE,
        )
        stanza_match = re.search(
            r"(?:ESTROFA|PRIMERA ESTROFA|STANZA)\s*:\s*\n?(.*)",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

        if not title_match or not theme_match or not stanza_match:
            raise DefectivePoemResponseError(
                "La respuesta del modelo no contiene la estructura requerida (TITULO, TEMATICA, ESTROFA)."
            )

        title = re.sub(r"^[\*\-\"']+|[\*\-\"']+$", "", title_match.group(1).strip()).strip()
        theme = re.sub(r"^[\*\-\"']+|[\*\-\"']+$", "", theme_match.group(1).strip()).strip()
        raw_stanza = stanza_match.group(1).strip()

        if len(title) < 2 or len(title) > 200:
            raise DefectivePoemResponseError(f"Título inválido generado por el modelo: '{title}'.")
        if len(theme) < 4:
            raise DefectivePoemResponseError(f"Temática inválida generada por el modelo: '{theme}'.")

        stanza_clean = self._validate_single_stanza(raw_stanza)
        return title, theme, stanza_clean

    def _parse_and_validate_continuation_stanza(
        self,
        *,
        raw_content: str,
        title: str,
        previous_stanzas: list[str],
    ) -> str:
        """
        Valida que la respuesta de continuación contenga una única estrofa válida
        y que NO sustituya ni repita las estrofas anteriores del poema (Escenario D).
        """
        if not raw_content or not raw_content.strip():
            raise DefectivePoemResponseError("El modelo devolvió una estrofa vacía.")

        cleaned = self._strip_code_fences(raw_content.strip())

        # Eliminar encabezados accidentales como "Estrofa 2:" o "ESTROFA:"
        cleaned = re.sub(
            r"^\s*(?:ESTROFA(?:\s+\d+)?|STANZA(?:\s+\d+)?)\s*:\s*\n?",
            "",
            cleaned,
            flags=re.IGNORECASE,
        ).strip()

        # Verificar que no haya devuelto múltiples bloques de estrofas ni sustituido el poema
        stanza_clean = self._validate_single_stanza(cleaned)

        # Verificar que no esté repitiendo ninguna estrofa previa
        normalized_new = self._normalize_for_comparison(stanza_clean)
        for idx, prev in enumerate(previous_stanzas, start=1):
            normalized_prev = self._normalize_for_comparison(prev)
            if not normalized_prev:
                continue
            if normalized_new == normalized_prev or normalized_prev in normalized_new:
                raise DefectivePoemResponseError(
                    f"La respuesta del modelo intenta repetir o sustituir la estrofa previa #{idx}."
                )

        # Verificar que no incluya el título como primera línea
        first_line = stanza_clean.splitlines()[0].strip()
        if self._normalize_for_comparison(first_line) == self._normalize_for_comparison(title):
            raise DefectivePoemResponseError(
                "La respuesta del modelo incluye el título del poema en lugar de limitarse a la nueva estrofa."
            )

        return stanza_clean

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remueve bloques markdown ``` si el modelo los incluyó alrededor del texto."""
        pattern = r"^```[a-zA-Z]*\s*\n(.*?)\n```$"
        match = re.match(pattern, text.strip(), flags=re.DOTALL)
        if match:
            return match.group(1).strip()
        return text.strip()

    @staticmethod
    def _validate_single_stanza(stanza_text: str) -> str:
        """
        Verifica que el texto corresponda a una única estrofa poética coherente:
        - Al menos 2 versos y mínimo 20 caracteres.
        - No contiene múltiples estrofas separadas por saltos de línea dobles.
        - No contiene frases de meta-comentario conversacional de IA.
        """
        candidate = stanza_text.strip()
        if not candidate:
            raise DefectivePoemResponseError("La estrofa generada está vacía.")

        # Si hay bloques separados por líneas en blanco, rechazar por contener múltiples estrofas
        blocks = [b.strip() for b in re.split(r"\n\s*\n", candidate) if b.strip()]
        if len(blocks) > 1:
            raise DefectivePoemResponseError(
                f"Se esperaba una única estrofa por intervención, pero el modelo devolvió {len(blocks)} bloques."
            )

        lines = [line.strip() for line in blocks[0].splitlines() if line.strip()]
        if len(lines) < 2:
            raise DefectivePoemResponseError(
                "La estrofa generada debe contener al menos 2 versos poéticos."
            )
        if len(lines) > 12:
            raise DefectivePoemResponseError(
                f"La estrofa generada excede la extensión máxima permitida ({len(lines)} versos)."
            )

        joined = "\n".join(lines)
        if len(joined) < 20:
            raise DefectivePoemResponseError("La estrofa generada es demasiado corta para ser válida.")

        forbidden_prefixes = (
            "aquí tienes",
            "claro,",
            "por supuesto",
            "a continuación",
            "espero que te guste",
            "nota:",
        )
        lower_joined = joined.lower()
        for prefix in forbidden_prefixes:
            if lower_joined.startswith(prefix):
                raise DefectivePoemResponseError(
                    f"La respuesta contiene comentarios externos no permitidos ('{prefix}')."
                )

        return joined

    @staticmethod
    def _normalize_for_comparison(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip().lower())
