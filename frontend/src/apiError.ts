export class ApiError extends Error {
  code: string;
  details: string[];
  constructor(message: string, code: string, details: string[] = []) {
    super(message);
    this.code = code;
    this.details = details;
  }
}
