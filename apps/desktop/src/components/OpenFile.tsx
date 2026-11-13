import { useRef } from "react";
import { FileUp } from "lucide-react";

export function OpenFile({
  label,
  accept,
  disabled,
  onOpen,
}: {
  label: string;
  accept: string;
  disabled?: boolean;
  onOpen: (file: File) => void;
}) {
  const picker = useRef<HTMLInputElement>(null);
  return (
    <>
      <button
        className="secondary"
        type="button"
        disabled={disabled}
        onClick={() => picker.current?.click()}
      >
        <FileUp size={15} /> {label}
      </button>
      <input
        ref={picker}
        type="file"
        aria-label={label}
        accept={accept}
        disabled={disabled}
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          event.currentTarget.value = "";
          if (file) onOpen(file);
        }}
      />
    </>
  );
}
