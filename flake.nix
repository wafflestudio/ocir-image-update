{
  "description" = "Development shell for the OCIR image update Oracle Function";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = import nixpkgs { inherit system; };
      python = pkgs.python312.withPackages (ps: with ps; [
        cryptography
        pyjwt
        requests
      ]);
    in {
      devShells.${system}.default = pkgs.mkShell {
        packages = [
          python
        ];
      };
    };
}
