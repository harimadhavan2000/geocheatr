import google.generativeai as genai
import PIL.Image
import os
import sys
import mss          # For screen capture
import io           # For handling image bytes in memory
import tkinter as tk
from tkinter import ttk
from tkinter import scrolledtext, messagebox, Frame, Label, Button,IntVar, Entry
import threading    # To keep the GUI responsive during API calls
import time
import json # Add this import
import tkintermapview # Add this import

# --- Configuration ---
# Ensure GOOGLE_API_KEY is set as an environment variable
try:
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    if not GOOGLE_API_KEY:
        raise ValueError("GOOGLE_API_KEY environment variable not set.")
    genai.configure(api_key=GOOGLE_API_KEY)
except ValueError as e:
    messagebox.showerror("API Key Error", str(e))
    sys.exit("API key configuration failed.")
except Exception as e:
    messagebox.showerror("Configuration Error", f"An unexpected error occurred during configuration: {e}")
    sys.exit("Configuration failed.")

# --- Constants ---
FRAME_INTERVAL_MS = 5000 # Send a frame every 5 seconds when recording (adjust as needed)

# --- Prompts ---
PROMPT_FOR_FRAME = """
Analyze this single frame from a sequence exploring a location.
Focus *only* on new, distinct visual clues visible *in this specific frame*
(e.g., specific text on a sign, unique building feature, clear landscape element).
List these specific observations briefly. Do not guess the location yet or repeat
obvious general features like 'road' or 'sky' unless they are very distinctive.
Assume I have seen previous frames.
"""

PROMPT_FINAL_ANALYSIS = """
Based on our entire conversation history, including all the frames and clues
identified previously, provide your response structured in two parts:

First, provide the textual analysis:
1.  What is the most likely country and specific region/state?
2.  Can you suggest a more specific city or area?
3.  Summarize the key evidence from *across all frames* that led to your conclusion.
4.  State your overall confidence level (High, Medium, Low).

Second, provide a JSON object containing coordinate data, enclosed strictly
between "<<<JSON_START>>>" and "<<<JSON_END>>>".
The JSON object should be a list of dictionaries, where each dictionary represents
a potential location and contains the following keys:
- "latitude": float
- "longitude": float
- "radius_km": float (estimated radius of error in kilometers)
- "confidence": string ("High", "Medium", or "Low")
- "reason": string (brief reason for suggesting this coordinate)

Provide up to 5 potential coordinate locations that are significantly distinct
(non-overlapping areas). Sort the list by "radius_km" in ascending order.

Example of the JSON part:
<<<JSON_START>>>
[
  {
    "latitude": 48.8584,
    "longitude": 2.2945,
    "radius_km": 1.0,
    "confidence": "High",
    "reason": "Eiffel Tower clearly visible in multiple frames."
  },
  {
    "latitude": 51.5074,
    "longitude": -0.1278,
    "radius_km": 5.0,
    "confidence": "Medium",
    "reason": "Red double-decker buses and specific architecture suggest London area."
  }
]
<<<JSON_END>>>

Ensure the JSON is valid. Do not include any text after "<<<JSON_END>>>".
"""

# --- Screen Capture Function (to memory) ---
def capture_screen_to_image(monitor_number=1):
    """ Captures screen to PIL Image object. """
    try:
        with mss.mss() as sct:
            monitor = sct.monitors[monitor_number]
            sct_img = sct.grab(monitor)
            img = PIL.Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
            # print(f"Captured monitor {monitor_number} to memory.") # Less verbose
            return img
    except IndexError:
        print(f"Error: Monitor {monitor_number} not found.")
        # Avoid messagebox here as it can be called frequently from background thread
        return None
    except Exception as e:
        print(f"An error occurred during screen capture: {e}")
        return None

# --- GUI Application ---
class GeoAnalysisApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Geo Session Analyzer")
        self.root.geometry("700x600")

        self.monitor_to_capture = 1
        self.model = genai.GenerativeModel(model_name="gemini-2.5-flash-preview-04-17") # Or "gemini-1.5-pro"
        self.chat_session = None
        self.captured_frames = []
        self.state = "IDLE" # IDLE, RECORDING, PAUSED, ANALYZING
        self.frame_sender_job_id = None # To store the ID of the root.after job

        # --- Controls ---
        control_frame = Frame(root)
        control_frame.pack(pady=10, fill=tk.X)

        self.start_button = Button(control_frame, text="Start Session", command=self.start_session)
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.pause_resume_button = Button(control_frame, text="Pause", command=self.toggle_pause_resume, state=tk.DISABLED)
        self.pause_resume_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = Button(control_frame, text="Stop & Analyze", command=self.stop_and_analyze, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        self.clear_button = Button(control_frame, text="Clear History", command=self.clear_history)
        self.clear_button.pack(side=tk.LEFT, padx=5)

        # --- Status ---
        status_frame = Frame(root)
        status_frame.pack(pady=5, fill=tk.X)
        self.status_label = Label(status_frame, text="Status: IDLE", fg="blue")
        self.status_label.pack(side=tk.LEFT, padx=10)
        self.frame_count_label = Label(status_frame, text="Frames Sent: 0")
        self.frame_count_label.pack(side=tk.LEFT, padx=10)
        self.frame_count = 0


        # --- Results Display (Tabs) ---
        self.notebook = ttk.Notebook(root)

        # --- Analysis Tab Setup ---
        self.tab1_frame = Frame(self.notebook) # Frame for the first tab
        self.analysis_text = scrolledtext.ScrolledText(self.tab1_frame, wrap=tk.WORD, height=25, width=80)
        self.analysis_text.pack(fill=tk.BOTH, expand=True)
        self.analysis_text.insert(tk.END, "Instructions:\n1. Click 'Start Session' to begin.\n2. The app will capture screen frames periodically.\n3. Use 'Pause'/'Resume' as needed.\n4. Click 'Stop & Analyze' for the final location guess.\n5. 'Clear History' starts a completely new location analysis.\n")
        self.analysis_text.config(state=tk.DISABLED)
        self.notebook.add(self.tab1_frame, text='Analysis') # Add analysis tab first

        # --- Coordinates Tab Setup (using PanedWindow) ---
        # Create a PanedWindow for the second tab, oriented vertically
        self.tab2_pane = tk.PanedWindow(self.notebook, orient=tk.VERTICAL, sashrelief=tk.RAISED, sashwidth=5, background="lightgrey") # Added background for visibility
        self.notebook.add(self.tab2_pane, text='Coordinates') # Add coordinates tab second

        # Create the map widget and add it to the top pane of the PanedWindow
        # Give it a minimum size and allow it to expand
        self.map_widget = tkintermapview.TkinterMapView(self.tab2_pane, corner_radius=0)
        self.tab2_pane.add(self.map_widget, stretch="always", minsize=200) # Add to pane, stretch, set min height

        # Set a default starting position (e.g., world view)
        self.map_widget.set_position(0, 0) # Center on 0,0
        self.map_widget.set_zoom(1)       # Zoom out for world view

        # Create a small frame for the status label in the bottom pane
        self.status_label_frame = Frame(self.tab2_pane, height=20) # Fixed small height for label area
        self.tab2_pane.add(self.status_label_frame, stretch="never") # Add to pane, don't stretch

        # Add the status label inside the small frame
        self.coords_status_label = Label(self.status_label_frame, text="Coordinates will be plotted here after analysis.", justify=tk.LEFT, anchor="w")
        self.coords_status_label.pack(fill=tk.BOTH, expand=True, padx=5, pady=2) # Fill the small frame

        # Pack the notebook itself to fill the main window
        self.notebook.pack(pady=10, padx=10, fill=tk.BOTH, expand=True)

        self.update_button_states()

    def update_status(self, text=None, color="black"):
        """ Updates the status label safely. """
        if text:
            self.status_label.config(text=f"Status: {text}", fg=color)

    def update_frame_count(self):
        """ Updates the frame count label safely. """
        self.frame_count_label.config(text=f"Frames Sent: {self.frame_count}")

    def update_results(self, text, append=False, target_tab='analysis'):
        """ Safely updates the text widget of the Analysis tab OR the status label of the Coords tab. """
        if target_tab == 'analysis':
            text_widget = self.analysis_text
            text_widget.config(state=tk.NORMAL)
            if append:
                text_widget.insert(tk.END, text + "\n")
            else:
                text_widget.delete('1.0', tk.END)
                text_widget.insert(tk.END, text + "\n")
            text_widget.see(tk.END) # Scroll to the end
            text_widget.config(state=tk.DISABLED)
        elif target_tab == 'coords':
            # Update the status label below the map
            self.coords_status_label.config(text=text)
            # No further action needed for the label, so we can return
            return
        else:
            print(f"Warning: Invalid target_tab '{target_tab}' in update_results.")
            # Fallback or error handling if needed (e.g., default to analysis tab)
            # text_widget = self.analysis_text # Example fallback
            return # Or just return if invalid tab

        # This part below is now only executed for the 'analysis' tab
        # text_widget.config(state=tk.NORMAL)
        # if append:
        #     text_widget.insert(tk.END, text + "\n")
        # else:
        #     text_widget.delete('1.0', tk.END)
        #     text_widget.insert(tk.END, text + "\n")
        # text_widget.see(tk.END) # Scroll to the end
        # text_widget.config(state=tk.DISABLED)

    def update_button_states(self):
        """Enable/disable buttons based on the current state."""
        if self.state == "IDLE":
            self.start_button.config(state=tk.NORMAL)
            self.pause_resume_button.config(state=tk.DISABLED, text="Pause")
            self.stop_button.config(state=tk.DISABLED)
            self.clear_button.config(state=tk.NORMAL if self.chat_session else tk.DISABLED)
        elif self.state == "RECORDING":
            self.start_button.config(state=tk.DISABLED)
            self.pause_resume_button.config(state=tk.NORMAL, text="Pause")
            self.stop_button.config(state=tk.NORMAL)
            self.clear_button.config(state=tk.DISABLED)
        elif self.state == "PAUSED":
            self.start_button.config(state=tk.DISABLED)
            self.pause_resume_button.config(state=tk.NORMAL, text="Resume")
            self.stop_button.config(state=tk.NORMAL)
            self.clear_button.config(state=tk.DISABLED)
        elif self.state == "ANALYZING":
            self.start_button.config(state=tk.DISABLED)
            self.pause_resume_button.config(state=tk.DISABLED)
            self.stop_button.config(state=tk.DISABLED, text="Analyzing...")
            self.clear_button.config(state=tk.DISABLED)

    def start_session(self):
        """Starts a new analysis session."""
        if not self.chat_session:
            self.update_results("Starting new session...")
            # Start a new chat session (clears history implicitly)
            self.chat_session = self.model.start_chat(history=[])
            self.captured_frames = []
            self.frame_count = 0
            self.update_frame_count()
            print("New chat session started.")
        else:
             self.update_results("Resuming session...") # Or should clear? Let's assume resume
             print("Resuming existing chat session.")

        self.state = "RECORDING"
        self.update_status("RECORDING", "red")
        self.update_button_states()
        # Start sending frames periodically
        self.schedule_next_frame()

    def toggle_pause_resume(self):
        """Pauses or resumes the frame sending."""
        if self.state == "RECORDING":
            self.state = "PAUSED"
            self.update_status("PAUSED", "orange")
            # Cancel the scheduled frame sender
            if self.frame_sender_job_id:
                self.root.after_cancel(self.frame_sender_job_id)
                self.frame_sender_job_id = None
                print("Frame sending paused.")
        elif self.state == "PAUSED":
            self.state = "RECORDING"
            self.update_status("RECORDING", "red")
            # Resume sending frames
            self.schedule_next_frame()
            print("Frame sending resumed.")
        self.update_button_states()

    def schedule_next_frame(self):
        """Schedules the next frame capture and send."""
        if self.state == "RECORDING":
            # Schedule the send_frame_task to run after FRAME_INTERVAL_MS
            self.frame_sender_job_id = self.root.after(FRAME_INTERVAL_MS, self.send_frame_task)

    def send_frame_task(self):
        """Captures and sends a single frame in a background thread."""
        if self.state != "RECORDING":
            return # Stop if state changed while waiting

        # Run capture and API call in a thread to avoid blocking GUI
        thread = threading.Thread(target=self._send_frame_worker, daemon=True)
        thread.start()

        # Schedule the next frame capture regardless of thread completion
        # This keeps the timing consistent, though API calls might lag/overlap
        # if the interval is too short or API is slow.
        self.schedule_next_frame()

    def _send_frame_worker(self):
        """Worker function executed in a thread to send one frame."""
        if self.state != "RECORDING" or not self.chat_session:
             return

        print("Capturing frame...")
        img = capture_screen_to_image(self.monitor_to_capture)

        if img:
            try:
                print(f"Sending frame {self.frame_count + 1} to Gemini...")
                # Send the frame with the specific prompt for frame analysis
                # Use stream=True for potentially faster feedback if needed,
                # but here we just send and wait for the non-streamed response.
                response = self.chat_session.send_message([PROMPT_FOR_FRAME, img])
                # Optionally display brief confirmation or key clues from response.text
                # self.root.after(0, lambda: self.update_results(f"Frame {self.frame_count + 1}: {response.text[:100]}...", append=True))
                print(f"Received response for frame {self.frame_count + 1}.")
                self.captured_frames.append(img)
                self.frame_count += 1
                # Update GUI from main thread using root.after
                self.root.after(0, self.update_frame_count)

            except Exception as e:
                print(f"Error sending frame to Gemini: {e}")
                # Maybe update status bar with error?
                # self.root.after(0, lambda: self.update_status(f"API Error: {e}", "red"))
                # Consider adding retry logic or pausing on error
        else:
            print("Frame capture failed.")
            # self.root.after(0, lambda: self.update_status("Capture Error", "red"))


    def stop_and_analyze(self):
        """Stops recording and asks the model for the final analysis."""
        if self.state not in ["RECORDING", "PAUSED"]:
            return

        print("Stopping session and requesting final analysis.")
        self.state = "ANALYZING"
        self.update_status("ANALYZING", "purple")
        self.stop_button.config(text="Analyzing...") # Immediate feedback
        self.update_button_states() # Disable buttons

        # Cancel any pending frame sends
        if self.frame_sender_job_id:
            self.root.after_cancel(self.frame_sender_job_id)
            self.frame_sender_job_id = None

        # Run final analysis in a background thread
        thread = threading.Thread(target=self._final_analysis_worker, daemon=True)
        thread.start()

    def _final_analysis_worker(self):
        """Worker function for final analysis API call using all captured frames."""
        # --- Initial Checks (remain the same) ---
        if not self.chat_session:
            self.root.after(0, lambda: self.update_results("Error: No active session to analyze.", target_tab='analysis'))
            self.root.after(0, lambda: self.update_results("Error: No active session to analyze.", target_tab='coords')) # Update status label
            self.root.after(0, self._finalize_analysis_ui)
            return
        if not self.captured_frames:
            self.root.after(0, lambda: self.update_results("Error: No frames were captured during the session.", target_tab='analysis'))
            self.root.after(0, lambda: self.update_results("Error: No frames were captured during the session.", target_tab='coords')) # Update status label
            self.root.after(0, self._finalize_analysis_ui)
            return

        # --- Update Status ---
        self.root.after(0, lambda: self.update_results(f"\nSending {len(self.captured_frames)} captured frames and requesting final analysis...", append=True, target_tab='analysis'))
        self.root.after(0, lambda: self.update_results("Waiting for analysis...", target_tab='coords')) # Update status label
        # Clear previous markers from the map (ensure this runs in main thread)
        self.root.after(0, self.map_widget.delete_all_marker)

        try:
            # --- Construct and Send Message (remains the same) ---
            intro_text = f"Here are {len(self.captured_frames)} frames captured sequentially during exploration of a location. Please analyze them together with the following request:"
            message_parts = [intro_text]
            message_parts.extend(self.captured_frames)
            message_parts.append(PROMPT_FINAL_ANALYSIS)
            print(f"Sending {len(self.captured_frames)} frames and final prompt to Gemini...")
            final_response = self.chat_session.send_message(message_parts)
            full_text = final_response.text
            print("Received final analysis.")

            # --- Parse the response for JSON ---
            analysis_part = full_text # Default to full text
            coords_status_update = "No valid coordinate JSON found in response." # Default status message
            json_start_marker = "<<<JSON_START>>>"
            json_end_marker = "<<<JSON_END>>>"
            markers_plotted = False # Flag to track if plotting was successful

            start_index = full_text.find(json_start_marker)
            end_index = full_text.find(json_end_marker)

            if start_index != -1 and end_index != -1 and start_index < end_index:
                analysis_part = full_text[:start_index].strip()
                json_string = full_text[start_index + len(json_start_marker):end_index].strip()
                print("JSON block found. Attempting to parse...")

                try:
                    coordinate_data = json.loads(json_string)
                    print("JSON parsed successfully.")

                    # --- Define the plotting function ---
                    def plot_markers_on_map(data):
                        """Clears existing markers and plots new ones based on data."""
                        nonlocal markers_plotted # Allow modification of the outer scope flag
                        self.map_widget.delete_all_marker() # Clear again for safety
                        plotted_count = 0
                        first_marker = True
                        if isinstance(data, list) and data:
                            for i, item in enumerate(data):
                                try:
                                    lat = float(item.get('latitude')) # Use .get() and handle potential None
                                    lon = float(item.get('longitude'))
                                    rank = i + 1
                                    # Add marker with rank as text
                                    self.map_widget.set_marker(lat, lon, text=str(rank))
                                    print(f"Plotted marker {rank}: ({lat}, {lon})")
                                    plotted_count += 1
                                    if first_marker:
                                        # Set map position to the first valid marker
                                        self.map_widget.set_position(lat, lon)
                                        self.map_widget.set_zoom(10) # Zoom in on first marker
                                        first_marker = False
                                except (ValueError, TypeError, AttributeError) as marker_err:
                                     print(f"Skipping invalid marker data: {item}. Error: {marker_err}")
                            if plotted_count > 0:
                                self.update_results(f"Plotted {plotted_count} potential locations (ranked).", target_tab='coords')
                                markers_plotted = True # Set flag indicating success
                            else:
                                self.update_results("Parsed JSON, but no valid coordinates to plot.", target_tab='coords')
                        else:
                            print("Parsed JSON is not a non-empty list.")
                            self.update_results("Parsed JSON coordinate data is empty or not a list.", target_tab='coords')

                    # --- Schedule plotting from main thread ---
                    # Use lambda default argument to capture current coordinate_data
                    self.root.after(0, lambda data=coordinate_data: plot_markers_on_map(data))

                except json.JSONDecodeError as json_err:
                    print(f"Error parsing JSON: {json_err}")
                    coords_status_update = f"Error parsing JSON block: {json_err}\nRaw JSON:\n{json_string}"
                    # Update status label with error if JSON parsing fails
                    self.root.after(0, lambda: self.update_results(coords_status_update, target_tab='coords'))

            else:
                 print("Warning: JSON markers not found or in wrong order.")
                 coords_status_update = f"Warning: JSON markers not found.\nFull response in Analysis tab."
                 # Update status label if markers not found
                 self.root.after(0, lambda: self.update_results(coords_status_update, target_tab='coords'))


            # --- Update Analysis Tab ---
            # This always happens, regardless of JSON success
            self.root.after(0, lambda: self.update_results(analysis_part, target_tab='analysis'))

            # Note: The coords tab (status label) is updated within plot_markers_on_map
            # or in the error handling blocks above. No separate update needed here.

        except Exception as e:
            print(f"Error getting final analysis from Gemini: {e}")
            error_message = f"\n--- ANALYSIS ERROR ---\n{e}"
            # Show error in analysis tab and coords status label
            self.root.after(0, lambda: self.update_results(error_message, append=True, target_tab='analysis'))
            self.root.after(0, lambda: self.update_results(f"Analysis Error: {e}", target_tab='coords'))
        finally:
            # Reset state and UI elements
             self.root.after(0, self._finalize_analysis_ui)

    def _finalize_analysis_ui(self):
        """Resets the UI state after analysis is complete or failed."""
        self.state = "IDLE" # Ready for new session or clearing
        self.update_status("IDLE", "blue")
        self.stop_button.config(text="Stop & Analyze") # Reset button text
        self.update_button_states() # Re-enable/disable appropriate buttons


    def clear_history(self):
        """Clears the chat history, captured frames, and map markers."""
        if self.state not in ["IDLE", "ANALYZING"]: # Prevent clearing during active recording/pause
             messagebox.showwarning("Clear History", "Stop the current session before clearing history.")
             return

        print("Clearing chat history and resetting.")
        # Cancel any pending jobs (though should be none if IDLE)
        if self.frame_sender_job_id:
            self.root.after_cancel(self.frame_sender_job_id)
            self.frame_sender_job_id = None

        self.chat_session = None
        self.captured_frames = []
        self.frame_count = 0
        self.update_frame_count()
        self.state = "IDLE"
        self.update_status("IDLE", "blue")

        # Clear analysis text area
        self.update_results("History cleared. Ready for a new session.", target_tab='analysis')

        # Clear map markers and reset coords status label
        self.map_widget.delete_all_marker()
        self.update_results("Coordinates will be plotted here after analysis.", target_tab='coords')
        # Optional: Reset map view to default
        self.map_widget.set_position(0, 0)
        self.map_widget.set_zoom(1)

        self.update_button_states()


# --- Main Execution ---
if __name__ == "__main__":
    root = tk.Tk()
    app = GeoAnalysisApp(root)
    root.mainloop()
